"""Controller logic against fake datapaths (needs Ryu or os-ken installed; skipped otherwise).

Every OpenFlow message the controller builds is serialised with the framework's
real OpenFlow 1.3 encoder, so invalid matches/actions fail here, not on a switch.
"""
import pytest

compat = pytest.importorskip("controller.compat")

import config  # noqa: E402
from common.db import Store  # noqa: E402
from controller.compat import ether_types, ethernet, ipv4, ofproto_v1_3, packet, tcp  # noqa: E402
from controller.events import EventLog  # noqa: E402
from controller.fault_tolerance import FaultTolerance  # noqa: E402
from controller.health_monitor import HealthMonitor  # noqa: E402
from controller.load_balancer import LoadBalancer  # noqa: E402
from controller.routing import Routing, l4_info  # noqa: E402
from controller.security import SecurityManager  # noqa: E402
from controller.stats_collector import StatsCollector  # noqa: E402
from controller.topology_manager import TopologyManager  # noqa: E402
from ai.predictor import CongestionPredictor  # noqa: E402

try:
    from ryu.ofproto import ofproto_v1_3_parser as parser_mod
except ImportError:
    from os_ken.ofproto import ofproto_v1_3_parser as parser_mod


class FakeDP(object):
    def __init__(self, dpid):
        self.id = dpid
        self.ofproto = ofproto_v1_3
        self.ofproto_parser = parser_mod
        self.sent = []
        self._xid = 0

    def set_xid(self, msg):
        self._xid += 1
        msg.set_xid(self._xid)

    def send_msg(self, msg):
        if msg.xid is None:
            self.set_xid(msg)
        msg.serialize()            # raises on malformed messages
        self.sent.append(msg)

    def of_type(self, cls):
        return [m for m in self.sent if isinstance(m, cls)]


class FakeApp(object):
    def __init__(self, tmp_path):
        self.datapaths = {}
        self.store = Store(str(tmp_path / "sim.db"))
        self.event_log = EventLog(self.store)
        self.topo = TopologyManager()
        self.predictor = CongestionPredictor(model_path=None)
        self.routing = Routing(self)
        self.stats = StatsCollector(self)
        self.lb = LoadBalancer(self)
        self.health = HealthMonitor(self)
        self.security = SecurityManager(self)
        self.fault = FaultTolerance(self)


HOST_PORT_BASE = 10


@pytest.fixture
def app(tmp_path, monkeypatch):
    # never touch the real training CSV from tests
    monkeypatch.setattr(config, "STATS_CSV", str(tmp_path / "link_stats.csv"))
    a = FakeApp(tmp_path)
    next_port = {}
    for name in config.SWITCHES:
        dpid = int(name[1:])
        a.datapaths[dpid] = FakeDP(dpid)
        a.topo.add_switch(dpid, list(range(1, 20)))
    for s1, s2 in config.SWITCH_LINKS:
        d1, d2 = int(s1[1:]), int(s2[1:])
        p1 = next_port[d1] = next_port.get(d1, 0) + 1
        p2 = next_port[d2] = next_port.get(d2, 0) + 1
        a.topo.add_link(d1, p1, d2, p2)
    for i, (name, h) in enumerate(sorted(config.all_hosts().items())):
        a.topo.learn_host(h["mac"], h["ip"], int(h["switch"][1:]), HOST_PORT_BASE + i)
    for name in ("srv1", "srv2", "srv3"):
        a.health.state[name]["status"] = "healthy"
    assert a.topo.ready
    return a


def tcp_packet(src_host, dst_ip, dst_mac, sport, dport=80, bits=tcp.TCP_SYN, src_ip=None, src_mac=None):
    h = config.all_hosts()[src_host] if src_host else None
    pkt = packet.Packet()
    pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                       dst=dst_mac, src=src_mac or h["mac"]))
    pkt.add_protocol(ipv4.ipv4(src=src_ip or h["ip"], dst=dst_ip, proto=6))
    pkt.add_protocol(tcp.tcp(src_port=sport, dst_port=dport, bits=bits))
    pkt.serialize()
    parsed = packet.Packet(pkt.data)
    return pkt.data, parsed, parsed.get_protocol(ipv4.ipv4)


def client_request(app, client, sport):
    h = config.CLIENTS[client]
    data, pkt, ip_pkt = tcp_packet(client, config.VIP, config.VIP_MAC, sport)
    host = app.topo.host_by_ip(h["ip"])
    dp = app.datapaths[host["dpid"]]
    app.lb.handle_client_packet(dp, host["port"], data, ip_pkt, l4_info(pkt, ip_pkt))
    return dp


def test_round_robin_installs_rewrite_paths(app):
    dp = client_request(app, "h1", 40001)
    client_request(app, "h1", 40002)
    client_request(app, "h3", 40003)
    servers = [s.server for s in app.lb.sessions.values()]
    assert sorted(servers) == ["srv1", "srv2", "srv3"]
    kinds = sorted(r.kind for r in app.routing.records.values())
    assert kinds == ["lb_fwd"] * 3 + ["lb_rev"] * 3
    # ingress switch got a rule matching the VIP and a packet-out with the rewrite
    flow_mods = dp.of_type(parser_mod.OFPFlowMod)
    assert any(fm.match.get("ipv4_dst") == config.VIP for fm in flow_mods)
    outs = dp.of_type(parser_mod.OFPPacketOut)
    assert outs and any(getattr(a, "key", None) == "ipv4_dst" for a in outs[0].actions)
    # the reverse path's last hop restores the VIP as source
    rev = [r for r in app.routing.records.values() if r.kind == "lb_rev"][0]
    last_dp = app.datapaths[rev.hops[-1][0]]
    assert any(getattr(a, "key", None) == "ipv4_src" and a.value == config.VIP
               for fm in last_dp.of_type(parser_mod.OFPFlowMod) if fm.cookie == rev.cookie
               for inst in fm.instructions for a in inst.actions)


def test_session_is_sticky(app):
    client_request(app, "h2", 41000)
    first = list(app.lb.sessions.values())[0].server
    client_request(app, "h2", 41000)
    assert len(app.lb.sessions) == 1
    assert list(app.lb.sessions.values())[0].server == first


def test_no_server_when_pool_empty(app):
    for s in app.health.state.values():
        s["status"] = "down"
    client_request(app, "h1", 42000)
    assert not app.lb.sessions
    assert any(e["type"] == "lb_no_server" for e in app.event_log.recent())


def test_link_failure_reroutes_and_measures_recovery(app):
    client_request(app, "h1", 43000)
    rec = next(r for r in app.routing.records.values() if r.kind == "lb_fwd")
    a, b = rec.path[0], rec.path[1]
    app.fault.link_down(a, b, "test")
    new = next(r for r in app.routing.records.values() if r.kind == "lb_fwd")
    assert new.cookie != rec.cookie
    assert not any({x, y} == {a, b} for x, y in zip(new.path, new.path[1:]))
    barriers = [(dpid, m.xid) for dpid, dp in app.datapaths.items()
                for m in dp.of_type(parser_mod.OFPBarrierRequest)]
    assert barriers
    for _, xid in barriers:
        app.fault.on_barrier_reply(xid)
    assert app.fault.last_recovery_ms is not None
    assert any(e["type"] == "recovery" for e in app.event_log.recent())


def test_syn_flood_gets_blocked(app):
    host = app.topo.host_by_ip(config.CLIENTS["h4"]["ip"])
    blocked = False
    for i in range(200):
        blocked = app.security.on_new_flow(config.CLIENTS["h4"]["ip"], True, 100.0 + i * 0.005)
        if blocked:
            break
    assert blocked and app.security.is_blocked("10.0.0.4")
    dp = app.datapaths[host["dpid"]]
    drops = [fm for fm in dp.of_type(parser_mod.OFPFlowMod) if fm.priority == config.PRIORITY_BLOCK]
    assert drops and drops[0].hard_timeout == config.DDOS_BLOCK_SECONDS and not drops[0].instructions
    assert app.security.unblock("10.0.0.4")
    assert dp.of_type(parser_mod.OFPFlowMod)[-1].command == ofproto_v1_3.OFPFC_DELETE_STRICT


def test_servers_are_never_blocked(app):
    for i in range(200):
        assert not app.security.on_new_flow(config.SERVERS["srv1"]["ip"], True, 1 + i * 0.001)


def test_health_probe_and_reply(app):
    app.health.state["srv1"]["status"] = "unknown"
    host = app.topo.host_by_ip(config.SERVERS["srv1"]["ip"])
    sport = app.health._send_probe("srv1", host, 1.0)
    dp = app.datapaths[host["dpid"]]
    probe = packet.Packet(dp.of_type(parser_mod.OFPPacketOut)[-1].data)
    assert probe.get_protocol(tcp.tcp).dst_port == config.HEALTH_CHECK_PORT
    data, pkt, ip_pkt = tcp_packet(None, config.PROBE_IP, config.PROBE_MAC, 80, dport=sport,
                                   bits=tcp.TCP_SYN | tcp.TCP_ACK,
                                   src_ip=config.SERVERS["srv1"]["ip"], src_mac=config.SERVERS["srv1"]["mac"])
    app.health.handle_probe_reply(dp, pkt, ip_pkt)
    assert app.health.status("srv1") == "healthy"
    rst = packet.Packet(dp.of_type(parser_mod.OFPPacketOut)[-1].data).get_protocol(tcp.tcp)
    assert rst.bits & tcp.TCP_RST


def test_server_failure_evicts_sessions(app):
    for port in range(44000, 44006):
        client_request(app, "h1", port)
    before = sum(1 for s in app.lb.sessions.values() if s.server == "srv1")
    assert before > 0
    for _ in range(config.HEALTH_FAIL_THRESHOLD):
        app.health._result("srv1", False, "timeout", 1.0)
    assert app.health.status("srv1") == "down"
    assert not any(s.server == "srv1" for s in app.lb.sessions.values())
    assert "srv1" not in app.lb.candidates()


def test_stats_cycle_computes_utilisation_and_predictions(app):
    # Simulate two port-stats samples 2 s apart on the s3<->s5 link: 5 Mbit/s.
    link = app.topo.graph[3][5]["ports"]
    t = app.stats.ports
    t.update(3, link[3], 0.0, 0, 0, 0, 0, 0)
    t.update(3, link[3], 2.0, 1_250_000, 0, 1000, 0, 0)
    app.stats._compute(2.0)
    st = app.stats.link_stats["s3-s5"]
    assert round(st["util"]) == 50 and round(st["mbps"], 1) == 5.0
    assert app.stats.predictions["s3-s5"] == pytest.approx(50.0)
    assert app.topo.graph[3][5]["weight"] > 1.0
    app.routing.records.clear()
    app.stats.request_stats()   # serialises stats requests for every switch
    assert all(dp.of_type(parser_mod.OFPPortStatsRequest) for dp in app.datapaths.values())


def test_app_does_not_shadow_framework_attributes():
    """RyuApp/OSKenApp keep their incoming event queue in `self.events`; overwriting it
    silently stops all OpenFlow event delivery to the app."""
    import inspect
    import controller.sdn_controller as main
    source = inspect.getsource(main.SDNController)
    for name in ("events", "threads", "observers", "event_handlers", "name"):
        assert "self.%s =" % name not in source, "SDNController overwrites framework attribute %r" % name
