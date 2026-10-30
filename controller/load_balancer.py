"""VIP load balancer.

Clients talk to the virtual IP. For every new client connection the controller
chooses a real server with the active strategy and installs two paths:

  forward  client -> server: the client's edge switch rewrites dst VIP -> server
           (IP and MAC); the rest of the path matches the real server address.
  reverse  server -> client: the client's edge switch rewrites src server -> VIP,
           so the client only ever sees 10.0.0.100.

TCP/UDP are balanced per connection (5-tuple); other protocols (ICMP) per client.
Sessions stay pinned to their server for LB_SESSION_TTL seconds.
"""
import collections
import logging
import time

import config
from controller.lb_strategies import SelectionContext, make_strategy
from controller.routing import ETH_TYPE_IP, FlowRecord, l4_match_fields

LOG = logging.getLogger("sdn.lb")


class Session(object):
    def __init__(self, key, server):
        self.key = key                # (client_ip, proto, client_port, server_port)
        self.server = server
        self.created = time.time()
        self.last_seen = self.created
        self.fwd_cookie = None
        self.rev_cookie = None
        self.established = False      # counted once for the SYN/ACK ratio

    @property
    def client_ip(self):
        return self.key[0]


class LoadBalancer(object):
    def __init__(self, app):
        self.app = app
        self.servers = {name: dict(info) for name, info in config.SERVERS.items()}
        self.ip_to_server = {info["ip"]: name for name, info in self.servers.items()}
        self.strategy = make_strategy(config.DEFAULT_STRATEGY)
        self.sessions = {}                         # key -> Session
        self.total_assigned = collections.Counter()  # server -> connections ever assigned
        self._last_no_server_event = 0

    # ================================================================ strategy
    @property
    def strategy_name(self):
        return self.strategy.name

    def set_strategy(self, name):
        old = self.strategy.name
        self.strategy = make_strategy(name)  # raises ValueError for unknown names
        self.app.event_log.add("strategy_change", "info",
                            "Load-balancing strategy changed %s -> %s" % (old, name),
                            old=old, new=name)

    # ================================================================ selection
    def is_server_ip(self, ip):
        return ip in self.ip_to_server

    def candidates(self):
        return [name for name, info in self.servers.items()
                if self.app.health.in_pool(name) and self.app.topo.host_by_ip(info["ip"])]

    def build_context(self, client_dpid, candidates):
        stats = self.app.stats
        server_load, predicted, path_cong = {}, {}, {}
        for name in candidates:
            link = stats.server_link_id(name)
            server_load[name] = stats.link_util(link)
            predicted[name] = stats.predictions.get(link, server_load[name])
            host = self.app.topo.host_by_ip(self.servers[name]["ip"])
            path = self.app.routing.compute_path(client_dpid, host["dpid"]) if host else None
            worst = 0.0
            for a, b in zip(path or [], (path or [])[1:]):
                link_id = self.app.topo.link_id(a, b)
                worst = max(worst, stats.predictions.get(link_id, stats.link_util(link_id)))
            path_cong[name] = worst
        return SelectionContext(server_load=server_load, active_flows=self.active_flows(),
                                predicted_load=predicted, path_congestion=path_cong,
                                congestion_threshold=config.PREDICTION_CONGESTION_THRESHOLD)

    # ================================================================ packet handling
    @staticmethod
    def session_key(client_ip, proto, src_port, dst_port):
        if proto in (6, 17):
            return (client_ip, proto, src_port, dst_port)
        return (client_ip, proto, 0, 0)

    def handle_client_packet(self, dp, in_port, data, ip_pkt, l4):
        """A client packet to the VIP reached the controller (no rule yet)."""
        proto, sport, dport, _ = l4
        key = self.session_key(ip_pkt.src, proto, sport, dport)
        session = self.sessions.get(key)
        candidates = self.candidates()
        if session is None or session.server not in candidates:
            if not candidates:
                now = time.time()
                if now - self._last_no_server_event > 10:
                    self._last_no_server_event = now
                    self.app.event_log.add("lb_no_server", "error",
                                        "No healthy server in the pool; dropping VIP traffic")
                return
            server = self.strategy.select(candidates, self.build_context(dp.id, candidates))
            session = Session(key, server)
            self.sessions[key] = session
            self.total_assigned[server] += 1
            LOG.info("%s: %s:%s -> %s (%s)", self.strategy.name, ip_pkt.src, sport or "-",
                     server, "proto %d" % proto)
        session.last_seen = time.time()
        if not self.install_session(session, ingress_dpid=dp.id):
            return
        # Send the triggering packet on, rewritten, so the first packet is not lost.
        fwd = self.app.routing.records.get(session.fwd_cookie)
        if fwd is not None and fwd.ingress == dp.id:
            srv = self.servers[session.server]
            parser = dp.ofproto_parser
            actions = [parser.OFPActionSetField(eth_dst=srv["mac"]),
                       parser.OFPActionSetField(ipv4_dst=srv["ip"]),
                       parser.OFPActionOutput(fwd.hops[0][1])]
            self.app.routing.packet_out(dp, actions, data, in_port)

    def handle_server_reply(self, dp, in_port, data, ip_pkt, l4):
        """A server -> client packet missed its rule. Reinstall if it belongs to a session."""
        proto, sport, dport, _ = l4
        key = self.session_key(ip_pkt.dst, proto, dport, sport)
        session = self.sessions.get(key)
        if session is None or self.servers[session.server]["ip"] != ip_pkt.src:
            return False
        session.last_seen = time.time()
        if self.install_session(session):
            rev = self.app.routing.records.get(session.rev_cookie)
            if rev is not None and rev.ingress == dp.id:
                parser = dp.ofproto_parser
                actions = list(self._rev_rewrite(parser, len(rev.hops) == 1))
                actions.append(parser.OFPActionOutput(rev.hops[0][1]))
                self.app.routing.packet_out(dp, actions, data, in_port)
        return True

    @staticmethod
    def _rev_rewrite(parser, last_hop):
        if not last_hop:
            return []
        return [parser.OFPActionSetField(eth_src=config.VIP_MAC),
                parser.OFPActionSetField(ipv4_src=config.VIP)]

    # ================================================================ rule installation
    def install_session(self, session, ingress_dpid=None):
        """(Re)install forward and reverse paths. Returns True on success."""
        routing, topo = self.app.routing, self.app.topo
        client = topo.host_by_ip(session.client_ip)
        srv = self.servers[session.server]
        server = topo.host_by_ip(srv["ip"])
        if client is None or server is None:
            return False
        start = ingress_dpid if ingress_dpid is not None else client["dpid"]
        fwd_path = routing.compute_path(start, server["dpid"])
        rev_path = routing.compute_path(server["dpid"], client["dpid"])
        if fwd_path is None or rev_path is None:
            LOG.warning("no path between %s and %s", session.client_ip, session.server)
            return False

        for cookie in (session.fwd_cookie, session.rev_cookie):
            if cookie is not None:
                routing.remove_record(cookie)

        client_ip, proto, cport, sport = session.key
        fwd_l4 = l4_match_fields(proto, cport, sport)
        rev_l4 = l4_match_fields(proto, sport, cport)

        # Reverse first, so replies already have a path when the server answers.
        rev_hops = routing.hops_for(rev_path, client["port"])
        rev_cookie = routing.next_cookie()
        rev_match = dict(rev_l4, eth_type=ETH_TYPE_IP, ipv4_src=srv["ip"], ipv4_dst=client_ip)
        last = len(rev_hops) - 1
        routing.install_hops(rev_hops, lambda i: rev_match,
                             lambda i, parser: self._rev_rewrite(parser, i == last),
                             config.PRIORITY_LB, rev_cookie)

        fwd_hops = routing.hops_for(fwd_path, server["port"])
        fwd_cookie = routing.next_cookie()
        first_match = dict(fwd_l4, eth_type=ETH_TYPE_IP, ipv4_src=client_ip, ipv4_dst=config.VIP)
        rest_match = dict(fwd_l4, eth_type=ETH_TYPE_IP, ipv4_src=client_ip, ipv4_dst=srv["ip"])

        def fwd_actions(i, parser):
            if i == 0:
                return [parser.OFPActionSetField(eth_dst=srv["mac"]),
                        parser.OFPActionSetField(ipv4_dst=srv["ip"])]
            return []

        routing.install_hops(fwd_hops, lambda i: first_match if i == 0 else rest_match,
                             fwd_actions, config.PRIORITY_LB, fwd_cookie)

        routing.register(FlowRecord(fwd_cookie, "lb_fwd", client_ip, srv["ip"], fwd_path,
                                    fwd_hops, session.key))
        routing.register(FlowRecord(rev_cookie, "lb_rev", srv["ip"], client_ip, rev_path,
                                    rev_hops, session.key))
        session.fwd_cookie, session.rev_cookie = fwd_cookie, rev_cookie
        return True

    def reinstall_session(self, key):
        """Used after a link failure. Returns the set of dpids that got new rules."""
        session = self.sessions.get(key)
        if session is None or not self.install_session(session):
            return set()
        touched = set()
        for cookie in (session.fwd_cookie, session.rev_cookie):
            record = self.app.routing.records.get(cookie)
            if record is not None:
                touched |= record.dpids()
        return touched

    # ================================================================ lifecycle
    def on_record_removed(self, record):
        """Ingress rule of an LB path expired; drop the paired direction too."""
        session = self.sessions.get(record.session_key) if record.session_key else None
        if session is None:
            return
        if record.cookie == session.fwd_cookie:
            session.fwd_cookie = None
        elif record.cookie == session.rev_cookie:
            session.rev_cookie = None

    def evict_server(self, name):
        """Server left the pool: remove its rules so clients get re-balanced."""
        evicted = 0
        for key, session in list(self.sessions.items()):
            if session.server != name:
                continue
            for cookie in (session.fwd_cookie, session.rev_cookie):
                if cookie is not None:
                    self.app.routing.remove_record(cookie)
            del self.sessions[key]
            evicted += 1
        if evicted:
            LOG.info("evicted %d sessions from %s", evicted, name)
        return evicted

    def cleanup(self, now):
        for key, session in list(self.sessions.items()):
            alive = session.fwd_cookie in self.app.routing.records
            if not alive and now - session.last_seen > config.LB_SESSION_TTL:
                del self.sessions[key]

    # ================================================================ views
    def live_sessions(self):
        records = self.app.routing.records
        return [s for s in self.sessions.values() if s.fwd_cookie in records]

    def active_flows(self):
        counts = {name: 0 for name in self.servers}
        for session in self.live_sessions():
            counts[session.server] += 1
        return counts

    def clients_by_server(self):
        mapping = {name: set() for name in self.servers}
        for session in self.live_sessions():
            mapping[session.server].add(session.client_ip)
        return {name: sorted(ips) for name, ips in mapping.items()}
