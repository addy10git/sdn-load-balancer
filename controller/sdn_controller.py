"""
Intelligent SDN Load Balancer - main Ryu application.

Run:  ryu-manager --observe-links --ofp-tcp-listen-port 6653 --wsapi-port 8080 \
          controller.sdn_controller

This class wires OpenFlow events to the feature modules:
    topology_manager  LLDP discovery -> networkx graph, host locations
    routing           ARP / proxy-ARP, Dijkstra paths, flow registry
    load_balancer     VIP 10.0.0.100, three strategies, address rewriting
    stats_collector   port/flow stats every 2 s, utilisation, CSV + SQLite
    fault_tolerance   reroute on link failure, recovery time
    health_monitor    TCP-SYN health checks, standby auto-scaling
    security          DDoS detection and edge drop rules
    rest_api          JSON API for the dashboard
"""
import logging
import time

import config
from ai.predictor import CongestionPredictor
from common.db import Store
from common.logsetup import setup_logging
from controller.compat import (BACKEND, CONFIG_DISPATCHER, BaseApp, DEAD_DISPATCHER, MAIN_DISPATCHER,
                               WSGIApplication, arp, ether_types, ethernet, hub,
                               ipv4, ofp_event, ofproto_v1_3, packet, set_ev_cls, topo_event)
from controller.events import EventLog
from controller.fault_tolerance import FaultTolerance
from controller.health_monitor import HealthMonitor
from controller.load_balancer import LoadBalancer
from controller.rest_api import APP_KEY, RestController
from controller.routing import COOKIE_BASE, Routing, l4_info
from controller.security import BLOCK_COOKIE, SecurityManager
from controller.stats_collector import StatsCollector
from controller.topology_manager import TopologyManager

LOG = logging.getLogger("sdn.controller")
IGNORED_ETHERTYPES = (ether_types.ETH_TYPE_LLDP, ether_types.ETH_TYPE_IPV6)


class SDNController(BaseApp):
    OFP_VERSIONS = [ofproto_v1_3.OFP_VERSION]
    _CONTEXTS = {"wsgi": WSGIApplication}

    def __init__(self, *args, **kwargs):
        super(SDNController, self).__init__(*args, **kwargs)
        log_file = setup_logging("controller")
        self.started = time.time()
        self.datapaths = {}

        self.store = Store(config.DB_PATH)
        try:
            self.store.prune(config.DB_RETENTION_DAYS)
        except Exception as exc:
            LOG.warning("DB prune failed: %s", exc)
        self.event_log = EventLog(self.store)
        self.topo = TopologyManager()
        self.predictor = CongestionPredictor()
        self.routing = Routing(self)
        self.stats = StatsCollector(self)
        self.lb = LoadBalancer(self)
        self.health = HealthMonitor(self)
        self.security = SecurityManager(self)
        self.fault = FaultTolerance(self)

        kwargs["wsgi"].register(RestController, {APP_KEY: self})
        self.event_log.add("controller_start", "info",
                        "Controller started (%s, strategy %s, predictor %s)"
                        % (BACKEND, self.lb.strategy_name, self.predictor.model_name))
        LOG.info("logging to %s", log_file)

        self.threads.append(hub.spawn(self._loop, "stats", config.STATS_INTERVAL, self._stats_tick))
        self.threads.append(hub.spawn(self._loop, "health", config.HEALTH_CHECK_INTERVAL,
                                      self.health.run_cycle))

    # ================================================================ background loops
    @staticmethod
    def _loop(name, interval, func):
        while True:
            try:
                func()
            except Exception:
                LOG.exception("%s loop iteration failed", name)
            hub.sleep(interval)

    def _stats_tick(self):
        self.stats.run_cycle()
        self.lb.cleanup(time.time())

    # ================================================================ switch lifecycle
    @set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
    def _switch_features(self, ev):
        dp = ev.msg.datapath
        ofp, parser = dp.ofproto, dp.ofproto_parser
        # Remove rules a previous controller run left behind (only our cookies).
        self.routing.delete_cookie_prefix(dp, COOKIE_BASE)
        self.routing.delete_cookie_prefix(dp, BLOCK_COOKIE)
        # Table-miss: send to controller without buffering.
        self.routing.add_flow(dp, config.PRIORITY_TABLE_MISS, parser.OFPMatch(),
                              [parser.OFPActionOutput(ofp.OFPP_CONTROLLER, ofp.OFPCML_NO_BUFFER)])
        # Hosts' IPv6 neighbour discovery is noise for this IPv4 project.
        self.routing.add_flow(dp, config.PRIORITY_DROP_IPV6,
                              parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IPV6), [])
        # dp.id may not be set yet (the framework sets it in its own handler), so use the message.
        LOG.info("switch s%d configured (table-miss installed)", ev.msg.datapath_id)

    @set_ev_cls(ofp_event.EventOFPStateChange, [MAIN_DISPATCHER, DEAD_DISPATCHER])
    def _state_change(self, ev):
        dp = ev.datapath
        if ev.state == MAIN_DISPATCHER and dp.id is not None:
            self.datapaths[dp.id] = dp
        elif ev.state == DEAD_DISPATCHER and dp.id in self.datapaths:
            del self.datapaths[dp.id]
            self.stats.ports.forget_dpid(dp.id)
            self.event_log.add("switch_down", "error", "Switch s%d disconnected" % dp.id, dpid=dp.id)

    # ================================================================ topology events (LLDP)
    @set_ev_cls(topo_event.EventSwitchEnter)
    def _switch_enter(self, ev):
        sw = ev.switch
        self.topo.add_switch(sw.dp.id, [p.port_no for p in sw.ports])
        self.event_log.add("switch_up", "info", "Switch s%d connected" % sw.dp.id, dpid=sw.dp.id)

    @set_ev_cls(topo_event.EventSwitchLeave)
    def _switch_leave(self, ev):
        self.topo.remove_switch(ev.switch.dp.id)

    @set_ev_cls(topo_event.EventLinkAdd)
    def _link_add(self, ev):
        src, dst = ev.link.src, ev.link.dst
        was_ready = self.topo.ready
        if self.topo.add_link(src.dpid, src.port_no, dst.dpid, dst.port_no) and was_ready:
            self.fault.link_up(src.dpid, dst.dpid)

    @set_ev_cls(topo_event.EventLinkDelete)
    def _link_delete(self, ev):
        self.fault.link_down(ev.link.src.dpid, ev.link.dst.dpid, "LLDP link delete")

    @set_ev_cls(ofp_event.EventOFPPortStatus, MAIN_DISPATCHER)
    def _port_status(self, ev):
        msg = ev.msg
        dp, ofp = msg.datapath, msg.datapath.ofproto
        port = msg.desc.port_no
        if msg.reason == ofp.OFPPR_ADD:
            self.topo.add_port(dp.id, port)
            return
        neighbour = self.topo.link_at_port(dp.id, port)
        down = (msg.reason == ofp.OFPPR_DELETE
                or bool(msg.desc.state & ofp.OFPPS_LINK_DOWN)
                or bool(msg.desc.config & ofp.OFPPC_PORT_DOWN))
        if msg.reason == ofp.OFPPR_DELETE:
            self.topo.remove_port(dp.id, port)
        if down and neighbour is not None:
            self.fault.link_down(dp.id, neighbour, "port s%d:%d down" % (dp.id, port))

    # ================================================================ data plane
    @set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
    def _packet_in(self, ev):
        msg = ev.msg
        dp = msg.datapath
        in_port = msg.match["in_port"]
        pkt = packet.Packet(msg.data)
        eth = pkt.get_protocol(ethernet.ethernet)
        if eth is None or eth.ethertype in IGNORED_ETHERTYPES:
            return
        if not self.topo.ready:
            return  # until LLDP has mapped the inter-switch ports we cannot learn hosts safely

        arp_pkt = pkt.get_protocol(arp.arp)
        ip_pkt = pkt.get_protocol(ipv4.ipv4)
        at_edge = not self.topo.is_inter_switch_port(dp.id, in_port)

        if at_edge and eth.src not in (config.VIP_MAC, config.PROBE_MAC):
            src_ip = arp_pkt.src_ip if arp_pkt else (ip_pkt.src if ip_pkt else None)
            if src_ip in (config.VIP, config.PROBE_IP, "0.0.0.0"):
                src_ip = None
            self.topo.learn_host(eth.src, src_ip, dp.id, in_port)

        if arp_pkt is not None:
            self.routing.handle_arp(dp, in_port, arp_pkt, msg.data)
            return
        if ip_pkt is None:
            return
        if ip_pkt.dst == config.PROBE_IP:
            self.health.handle_probe_reply(dp, pkt, ip_pkt)
            return
        if self.security.is_blocked(ip_pkt.src):
            return

        l4 = l4_info(pkt, ip_pkt)
        if at_edge and self.security.on_new_flow(ip_pkt.src, l4[3], time.time()):
            return
        if ip_pkt.dst == config.VIP:
            self.lb.handle_client_packet(dp, in_port, msg.data, ip_pkt, l4)
            return
        if self.lb.is_server_ip(ip_pkt.src) and self.lb.handle_server_reply(dp, in_port, msg.data,
                                                                            ip_pkt, l4):
            return
        self.routing.handle_ip(dp, in_port, msg.data, ip_pkt)

    @set_ev_cls(ofp_event.EventOFPFlowRemoved, MAIN_DISPATCHER)
    def _flow_removed(self, ev):
        self.routing.on_flow_removed(ev.msg.datapath.id, ev.msg.cookie)

    @set_ev_cls(ofp_event.EventOFPBarrierReply, MAIN_DISPATCHER)
    def _barrier_reply(self, ev):
        self.fault.on_barrier_reply(ev.msg.xid)

    @set_ev_cls(ofp_event.EventOFPPortStatsReply, MAIN_DISPATCHER)
    def _port_stats_reply(self, ev):
        self.stats.on_port_stats(ev.msg)

    @set_ev_cls(ofp_event.EventOFPFlowStatsReply, MAIN_DISPATCHER)
    def _flow_stats_reply(self, ev):
        self.stats.on_flow_stats(ev.msg)

    # ================================================================ REST views
    def api_health(self):
        return {"status": "ok", "backend": BACKEND, "uptime": round(time.time() - self.started, 1),
                "switches": len(self.datapaths), "topology_ready": self.topo.ready}

    def _host_view(self, mac, host):
        ip = host.get("ip")
        name = config.host_name_by_ip(ip) or mac
        role = "client" if name in config.CLIENTS else "server" if name in config.SERVERS else "host"
        view = {"id": name, "ip": ip, "mac": mac, "switch": "s%d" % host["dpid"],
                "port": host["port"], "role": role, "blocked": self.security.is_blocked(ip)}
        if role == "server":
            view["standby"] = config.SERVERS[name]["standby"]
            view["status"] = self.health.status(name)
            view["in_pool"] = self.health.in_pool(name)
        return view

    def api_topology(self):
        stats, threshold = self.stats, config.PREDICTION_CONGESTION_THRESHOLD
        switches = [{"id": "s%d" % d, "dpid": d, "connected": d in self.datapaths}
                    for d in sorted(self.topo.graph.nodes)]
        hosts = [self._host_view(mac, h) for mac, h in list(self.topo.hosts.items())]
        links = []

        def link_view(link_id, source, target, kind, extra):
            st = stats.link_stats.get(link_id, {})
            predicted = stats.predictions.get(link_id)
            view = {"id": link_id, "source": source, "target": target, "type": kind,
                    "util": round(st.get("util", 0.0), 2), "mbps": round(st.get("mbps", 0.0), 3),
                    "drops": st.get("drops", 0), "flows": st.get("flows", 0),
                    "predicted": predicted,
                    "predicted_congested": predicted is not None and predicted > threshold}
            view.update(extra)
            return view

        for a, b, data in self.topo.links():
            links.append(link_view(self.topo.link_id(a, b), "s%d" % a, "s%d" % b, "switch",
                                   {"weight": round(data.get("weight", 1.0), 2),
                                    "ports": {"s%d" % k: v for k, v in data["ports"].items()}}))
        for host in hosts:
            dpid = int(host["switch"][1:])
            links.append(link_view(stats.host_link_id(dpid, host["id"]), host["switch"], host["id"],
                                   "host", {"ports": {host["switch"]: host["port"]}}))
        return {"switches": switches, "hosts": hosts, "links": links,
                "ready": self.topo.ready, "vip": config.VIP}

    @staticmethod
    def _series(samples, points, *fields):
        samples = list(samples)[-points:]
        return {name: [round(s[i], 3) for s in samples] for i, name in enumerate(fields)}

    def api_stats(self, points=90):
        links = {}
        for link_id, st in self.stats.link_stats.items():
            history = self.stats.history.get(link_id, [])
            links[link_id] = dict(st, util=round(st["util"], 2), mbps=round(st["mbps"], 3),
                                  history=self._series(history, points, "ts", "util", "mbps"))
        return {"interval": config.STATS_INTERVAL, "ts": self.stats.last_cycle, "links": links,
                "total_mbps": round(self.stats.total_mbps, 3),
                "total_history": self._series(self.stats.total_history, points, "ts", "mbps")}

    def api_servers(self):
        flows = self.lb.active_flows()
        clients = self.lb.clients_by_server()
        servers = []
        for name, info in config.SERVERS.items():
            st = self.health.state[name]
            status = st["status"]
            if info["standby"] and status == "healthy" and not self.health.standby_active:
                status = "standby"
            link = self.stats.server_link_id(name)
            servers.append({
                "name": name, "ip": info["ip"], "mac": info["mac"], "standby": info["standby"],
                "status": status, "health": st["status"], "in_pool": self.health.in_pool(name),
                "load": round(self.stats.server_load(name), 2),
                "predicted_load": self.stats.predictions.get(link),
                "active_flows": flows.get(name, 0),
                "clients": [config.host_name_by_ip(ip) or ip for ip in clients.get(name, [])],
                "total_assigned": self.lb.total_assigned[name],
                "rtt_ms": st["rtt_ms"], "last_check": st["last_check"], "reason": st["reason"],
            })
        return {"strategy": self.lb.strategy_name, "strategies": config.LB_STRATEGIES,
                "pool": self.health.pool(), "standby_active": self.health.standby_active,
                "avg_load": round(self.health.avg_load, 2), "servers": servers}

    def api_predictions(self, points=90):
        threshold = config.PREDICTION_CONGESTION_THRESHOLD
        links = {}
        for link_id, st in self.stats.link_stats.items():
            predicted = self.stats.predictions.get(link_id)
            links[link_id] = {
                "predicted": predicted, "actual": round(st["util"], 2),
                "congested": predicted is not None and predicted > threshold,
                "history": self._series(self.stats.pred_history.get(link_id, []), points,
                                        "ts", "actual", "predicted"),
            }
        return {"model": self.predictor.model_name, "fallback": self.predictor.using_fallback,
                "metrics": self.predictor.metrics, "threshold": threshold,
                "horizon_s": config.PREDICTION_HORIZON_SECONDS, "links": links}

    def api_summary(self):
        healthy = [n for n in config.SERVERS if self.health.status(n) == "healthy"]
        return {
            "total_mbps": round(self.stats.total_mbps, 3),
            "active_flows": sum(self.lb.active_flows().values()),
            "flow_paths": len(self.routing.records),
            "healthy_servers": len(healthy), "servers_total": len(config.SERVERS),
            "pool": self.health.pool(), "standby_active": self.health.standby_active,
            "strategy": self.lb.strategy_name,
            "attacks_blocked": self.security.attacks_blocked,
            "blocked_now": sorted(self.security.blocked),
            "last_recovery_ms": self.fault.last_recovery_ms,
            "recoveries": list(self.fault.history)[-10:],
            "uptime": round(time.time() - self.started, 1),
            "topology_ready": self.topo.ready, "switches": len(self.datapaths),
            "links": self.topo.graph.number_of_edges(), "hosts": len(self.topo.hosts),
            "model": self.predictor.model_name,
        }
