"""Routing: ARP handling, Dijkstra path installation and the flow registry.

Every multi-switch rule set we install is tracked as a FlowRecord with a unique
OpenFlow cookie. The cookie lets us
  * delete exactly the rules of one path (fault tolerance),
  * map flow statistics back to a source / server (stats, DDoS),
  * notice when the ingress rule expired (FlowRemoved).
"""
import itertools
import logging
import time

import config
from controller.compat import arp, ether_types, ethernet, packet, tcp, udp
from controller.path_utils import edge_key, hop_ports, path_uses_edge, shortest_path

LOG = logging.getLogger("sdn.routing")

ETH_TYPE_IP = ether_types.ETH_TYPE_IP
IPPROTO_TCP = 6
IPPROTO_UDP = 17
# All path cookies share this top byte so a restarted controller can delete
# its stale rules without touching the LLDP rule of the topology app.
COOKIE_BASE = 0x5D << 56
COOKIE_PREFIX_MASK = 0xFF << 56


class FlowRecord(object):
    """One installed path (a set of rules sharing a cookie)."""

    def __init__(self, cookie, kind, src_ip, dst_ip, path, hops, session_key=None):
        self.cookie = cookie
        self.kind = kind              # "route" | "lb_fwd" | "lb_rev"
        self.src_ip = src_ip
        self.dst_ip = dst_ip
        self.path = path              # [dpid, ...]
        self.hops = hops              # [(dpid, out_port), ...]
        self.session_key = session_key
        self.created = time.time()
        self.packets = 0              # latest packet_count at the ingress switch
        self.bytes = 0

    @property
    def ingress(self):
        return self.path[0]

    def dpids(self):
        return {d for d, _ in self.hops}


def l4_info(pkt, ip_pkt):
    """(proto, src_port, dst_port, is_syn) of an IPv4 packet."""
    if ip_pkt.proto == IPPROTO_TCP:
        t = pkt.get_protocol(tcp.tcp)
        if t is not None:
            is_syn = bool(t.bits & tcp.TCP_SYN) and not (t.bits & tcp.TCP_ACK)
            return IPPROTO_TCP, t.src_port, t.dst_port, is_syn
    if ip_pkt.proto == IPPROTO_UDP:
        u = pkt.get_protocol(udp.udp)
        if u is not None:
            return IPPROTO_UDP, u.src_port, u.dst_port, False
    return ip_pkt.proto, 0, 0, False


def l4_match_fields(proto, src_port, dst_port):
    """OFPMatch kwargs for the transport part of a flow."""
    fields = {"ip_proto": proto}
    if proto == IPPROTO_TCP:
        fields.update(tcp_src=src_port, tcp_dst=dst_port)
    elif proto == IPPROTO_UDP:
        fields.update(udp_src=src_port, udp_dst=dst_port)
    return fields


class Routing(object):
    def __init__(self, app):
        self.app = app
        self.topo = app.topo
        self.records = {}             # cookie -> FlowRecord
        self.route_index = {}         # (src_ip, dst_ip) -> cookie
        self._cookies = itertools.count(1)
        self._arp_sent = {}           # ip -> last ARP request ts (rate limit)

    # ================================================================ OpenFlow helpers
    def next_cookie(self):
        return COOKIE_BASE | next(self._cookies)

    @staticmethod
    def delete_cookie_prefix(dp, cookie_prefix):
        """Delete every rule whose cookie has the given top byte."""
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie_prefix, cookie_mask=COOKIE_PREFIX_MASK,
                                      table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))

    @staticmethod
    def add_flow(dp, priority, match, actions, idle=0, hard=0, cookie=0, send_removed=False):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        inst = [parser.OFPInstructionActions(ofp.OFPIT_APPLY_ACTIONS, actions)] if actions else []
        flags = ofp.OFPFF_SEND_FLOW_REM if send_removed else 0
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, priority=priority, match=match,
                                      instructions=inst, idle_timeout=idle, hard_timeout=hard,
                                      flags=flags))

    @staticmethod
    def delete_cookie(dp, cookie):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPFlowMod(datapath=dp, cookie=cookie, cookie_mask=0xFFFFFFFFFFFFFFFF,
                                      table_id=ofp.OFPTT_ALL, command=ofp.OFPFC_DELETE,
                                      out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                                      match=parser.OFPMatch()))

    @staticmethod
    def packet_out(dp, actions, data, in_port=None):
        ofp, parser = dp.ofproto, dp.ofproto_parser
        dp.send_msg(parser.OFPPacketOut(datapath=dp, buffer_id=ofp.OFP_NO_BUFFER,
                                        in_port=ofp.OFPP_CONTROLLER if in_port is None else in_port,
                                        actions=actions, data=data))

    def send_to_port(self, dpid, port, data):
        dp = self.app.datapaths.get(dpid)
        if dp is not None:
            self.packet_out(dp, [dp.ofproto_parser.OFPActionOutput(port)], data)

    def flood_to_edges(self, data, exclude=None):
        """Deliver a broadcast to every host-facing port of every switch.

        Broadcasts never travel over inter-switch links, so the looped mesh
        cannot produce a broadcast storm (the controller acts as the tree root).
        """
        for dpid in list(self.app.datapaths):
            for port in self.topo.edge_ports(dpid):
                if exclude == (dpid, port):
                    continue
                self.send_to_port(dpid, port, data)

    def install_hops(self, hops, match_for, actions_for, priority, cookie,
                     idle=config.FLOW_IDLE_TIMEOUT, hard=config.FLOW_HARD_TIMEOUT):
        """Install one rule per hop, egress first so the path is ready end to end
        by the time the ingress rule starts matching packets."""
        for index in reversed(range(len(hops))):
            dpid, out_port = hops[index]
            dp = self.app.datapaths.get(dpid)
            if dp is None:
                LOG.warning("s%d not connected, cannot install hop", dpid)
                continue
            parser = dp.ofproto_parser
            match = parser.OFPMatch(**match_for(index))
            actions = actions_for(index, parser) + [parser.OFPActionOutput(out_port)]
            self.add_flow(dp, priority, match, actions, idle, hard, cookie,
                          send_removed=(index == 0))

    # ================================================================ path computation
    def compute_path(self, src_dpid, dst_dpid):
        return shortest_path(self.topo.graph, src_dpid, dst_dpid)

    def hops_for(self, path, last_port):
        return hop_ports(self.topo.graph, path, last_port)

    # ================================================================ registry
    def register(self, record):
        self.records[record.cookie] = record
        if record.kind == "route":
            self.route_index[(record.src_ip, record.dst_ip)] = record.cookie
        return record

    def remove_record(self, cookie, delete_flows=True):
        """Forget a record and (optionally) delete its rules. Returns touched dpids."""
        record = self.records.pop(cookie, None)
        if record is None:
            return set()
        if record.kind == "route" and self.route_index.get((record.src_ip, record.dst_ip)) == cookie:
            del self.route_index[(record.src_ip, record.dst_ip)]
        touched = set()
        if delete_flows:
            for dpid in record.dpids():
                dp = self.app.datapaths.get(dpid)
                if dp is not None:
                    self.delete_cookie(dp, cookie)
                    touched.add(dpid)
        return touched

    def records_using_link(self, a, b):
        return [r for r in self.records.values() if path_uses_edge(r.path, a, b)]

    def on_flow_removed(self, dpid, cookie):
        """The ingress rule of a record expired: the whole path is considered gone."""
        record = self.records.get(cookie)
        if record is not None and record.ingress == dpid:
            self.remove_record(cookie, delete_flows=True)
            self.app.lb.on_record_removed(record)

    def flows_per_edge(self):
        """{canonical edge: number of records whose path crosses it}."""
        counts = {}
        for record in self.records.values():
            for a, b in zip(record.path, record.path[1:]):
                key = edge_key(a, b)
                counts[key] = counts.get(key, 0) + 1
        return counts

    # ================================================================ ARP
    def handle_arp(self, dp, in_port, arp_pkt, data):
        if arp_pkt.opcode == arp.ARP_REQUEST:
            if arp_pkt.src_ip == arp_pkt.dst_ip:
                return  # gratuitous ARP: already learnt by the caller
            target = arp_pkt.dst_ip
            if target == config.VIP:
                self.send_arp_reply(dp, in_port, config.VIP_MAC, target, arp_pkt)
            elif target == config.PROBE_IP:
                self.send_arp_reply(dp, in_port, config.PROBE_MAC, target, arp_pkt)
            else:
                host = self.topo.host_by_ip(target)
                if host is not None:
                    # Proxy ARP: answer from the controller, no broadcast needed.
                    self.send_arp_reply(dp, in_port, host["mac"], target, arp_pkt)
                else:
                    self.flood_to_edges(data, exclude=(dp.id, in_port))
        elif arp_pkt.opcode == arp.ARP_REPLY:
            if arp_pkt.dst_ip == config.PROBE_IP or arp_pkt.dst_mac == config.PROBE_MAC:
                return  # reply to one of our own discovery requests
            dst = self.topo.host_by_mac(arp_pkt.dst_mac) or self.topo.host_by_ip(arp_pkt.dst_ip)
            if dst is not None:
                self.send_to_port(dst["dpid"], dst["port"], data)

    def send_arp_reply(self, dp, port, resolved_mac, resolved_ip, request):
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst=request.src_mac, src=resolved_mac))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REPLY, src_mac=resolved_mac, src_ip=resolved_ip,
                                 dst_mac=request.src_mac, dst_ip=request.src_ip))
        pkt.serialize()
        self.packet_out(dp, [dp.ofproto_parser.OFPActionOutput(port)], pkt.data)

    def send_arp_request(self, target_ip, min_interval=1.0):
        """Ask "who has target_ip" from the controller's probe address (rate limited)."""
        now = time.time()
        if now - self._arp_sent.get(target_ip, 0) < min_interval:
            return
        self._arp_sent[target_ip] = now
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_ARP,
                                           dst="ff:ff:ff:ff:ff:ff", src=config.PROBE_MAC))
        pkt.add_protocol(arp.arp(opcode=arp.ARP_REQUEST, src_mac=config.PROBE_MAC,
                                 src_ip=config.PROBE_IP, dst_mac="00:00:00:00:00:00",
                                 dst_ip=target_ip))
        pkt.serialize()
        self.flood_to_edges(pkt.data)

    # ================================================================ IP routing
    def handle_ip(self, dp, in_port, data, ip_pkt):
        """Route a host-to-host IPv4 packet along the cheapest path."""
        dst = self.topo.host_by_ip(ip_pkt.dst)
        if dst is None:
            self.send_arp_request(ip_pkt.dst)
            return
        record = self.install_route(ip_pkt.src, ip_pkt.dst, dp.id)
        if record is not None:
            out_port = record.hops[0][1]
            self.packet_out(dp, [dp.ofproto_parser.OFPActionOutput(out_port)], data, in_port)

    def install_route(self, src_ip, dst_ip, ingress_dpid):
        dst = self.topo.host_by_ip(dst_ip)
        if dst is None:
            return None
        path = self.compute_path(ingress_dpid, dst["dpid"])
        if path is None:
            LOG.warning("no path s%d -> s%d for %s -> %s", ingress_dpid, dst["dpid"], src_ip, dst_ip)
            return None
        old = self.route_index.get((src_ip, dst_ip))
        if old is not None:
            self.remove_record(old)
        cookie = self.next_cookie()
        hops = self.hops_for(path, dst["port"])
        match = {"eth_type": ETH_TYPE_IP, "ipv4_src": src_ip, "ipv4_dst": dst_ip}
        self.install_hops(hops, lambda i: match, lambda i, parser: [],
                          config.PRIORITY_ROUTE, cookie)
        LOG.info("route %s -> %s via %s", src_ip, dst_ip, "-".join("s%d" % d for d in path))
        return self.register(FlowRecord(cookie, "route", src_ip, dst_ip, path, hops))
