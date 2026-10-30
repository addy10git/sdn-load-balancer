"""Server health checks and auto-scaling.

Health check (every HEALTH_CHECK_INTERVAL s): the controller itself crafts a TCP
SYN to port 80 of each server (source PROBE_IP) and injects it with a PacketOut
at the server's switch port. The server's answer comes back as a PacketIn:
    SYN-ACK  -> healthy (we then send a RST to close the half-open connection)
    RST      -> host up but HTTP service dead
    nothing  -> host / link down (timeout)
Servers whose location is still unknown are located with an ARP request.

Auto-scaling: when the average load of the pool stays above SCALE_UP_LOAD for
SCALE_UP_DURATION the standby server joins; it leaves again after the load
stays below SCALE_DOWN_LOAD for SCALE_DOWN_DURATION.
"""
import logging
import random
import time

import config
from controller.autoscaler import AutoScaler
from controller.compat import ether_types, ethernet, hub, ipv4, packet, tcp

LOG = logging.getLogger("sdn.health")


class HealthMonitor(object):
    def __init__(self, app):
        self.app = app
        self.state = {name: {"status": "unknown", "fails": 0, "last_ok": None, "rtt_ms": None,
                             "last_check": None, "reason": "not checked yet"}
                      for name in config.SERVERS}
        self.pending = {}               # probe source port -> (server, sent_ts)
        self.standby_active = False
        self.avg_load = 0.0
        self.scaler = AutoScaler(config.SCALE_UP_LOAD, config.SCALE_UP_DURATION,
                                 config.SCALE_DOWN_LOAD, config.SCALE_DOWN_DURATION)
        self._last_pool = None

    # ================================================================ pool membership
    def status(self, name):
        return self.state[name]["status"]

    def is_standby(self, name):
        return config.SERVERS[name].get("standby", False)

    def in_pool(self, name):
        if self.state[name]["status"] != "healthy":
            return False
        return not self.is_standby(name) or self.standby_active

    def pool(self):
        return sorted(n for n in config.SERVERS if self.in_pool(n))

    # ================================================================ probing
    def run_cycle(self):
        now = time.time()
        if not self.app.topo.ready:
            return
        sent = []
        for name, info in config.SERVERS.items():
            host = self.app.topo.host_by_ip(info["ip"])
            if host is None:
                self.app.routing.send_arp_request(info["ip"], min_interval=0.5)
                self._result(name, False, "location unknown (ARP sent)", now)
                continue
            sent.append(self._send_probe(name, host, now))
        if sent:
            hub.spawn_after(config.HEALTH_CHECK_TIMEOUT, self._expire, sent)
        self._autoscale(now)
        self._log_pool_change()

    def _send_probe(self, name, host, now):
        sport = random.randint(40000, 60000)
        while sport in self.pending:
            sport = random.randint(40000, 60000)
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                           dst=host["mac"], src=config.PROBE_MAC))
        pkt.add_protocol(ipv4.ipv4(src=config.PROBE_IP, dst=config.SERVERS[name]["ip"],
                                   proto=6, ttl=64))
        pkt.add_protocol(tcp.tcp(src_port=sport, dst_port=config.HEALTH_CHECK_PORT,
                                 seq=random.randint(0, 2 ** 31), ack=0,
                                 bits=tcp.TCP_SYN, window_size=1024))
        pkt.serialize()
        self.pending[sport] = (name, now)
        self.app.routing.send_to_port(host["dpid"], host["port"], pkt.data)
        return sport

    def handle_probe_reply(self, dp, pkt, ip_pkt):
        """PacketIn addressed to PROBE_IP: the answer to one of our probes."""
        seg = pkt.get_protocol(tcp.tcp)
        if seg is None:
            return
        entry = self.pending.pop(seg.dst_port, None)
        if entry is None:
            return
        name, sent_ts = entry
        now = time.time()
        if (seg.bits & tcp.TCP_SYN) and (seg.bits & tcp.TCP_ACK):
            self.state[name]["rtt_ms"] = round((now - sent_ts) * 1000.0, 2)
            self._result(name, True, "SYN-ACK received", now)
            self._send_rst(dp, ip_pkt, seg)
        elif seg.bits & tcp.TCP_RST:
            self._result(name, False, "connection refused (service down)", now)

    def _send_rst(self, dp, ip_pkt, seg):
        host = self.app.topo.host_by_ip(ip_pkt.src)
        if host is None:
            return
        pkt = packet.Packet()
        pkt.add_protocol(ethernet.ethernet(ethertype=ether_types.ETH_TYPE_IP,
                                           dst=host["mac"], src=config.PROBE_MAC))
        pkt.add_protocol(ipv4.ipv4(src=config.PROBE_IP, dst=ip_pkt.src, proto=6, ttl=64))
        pkt.add_protocol(tcp.tcp(src_port=seg.dst_port, dst_port=seg.src_port, seq=seg.ack,
                                 ack=0, bits=tcp.TCP_RST, window_size=0))
        pkt.serialize()
        self.app.routing.send_to_port(host["dpid"], host["port"], pkt.data)

    def _expire(self, ports):
        now = time.time()
        for sport in ports:
            entry = self.pending.pop(sport, None)
            if entry is not None:
                self._result(entry[0], False, "no reply within %.1fs" % config.HEALTH_CHECK_TIMEOUT, now)
        self._log_pool_change()

    # ================================================================ state machine
    def _result(self, name, ok, reason, now):
        st = self.state[name]
        st["last_check"], st["reason"] = now, reason
        events = self.app.event_log
        if ok:
            st["fails"], st["last_ok"] = 0, now
            if st["status"] != "healthy":
                previous = st["status"]
                st["status"] = "healthy"
                if previous == "down":
                    events.add("server_up", "success", "%s recovered (%s)" % (name, reason), server=name)
                else:
                    events.add("server_healthy", "info", "%s is healthy" % name, server=name)
        else:
            st["fails"] += 1
            if st["fails"] >= config.HEALTH_FAIL_THRESHOLD and st["status"] != "down":
                st["status"] = "down"
                events.add("server_down", "error", "%s failed health check: %s" % (name, reason),
                           server=name)
                self.app.lb.evict_server(name)

    def _log_pool_change(self):
        pool = self.pool()
        if pool != self._last_pool:
            if self._last_pool is not None:
                added = sorted(set(pool) - set(self._last_pool))
                removed = sorted(set(self._last_pool) - set(pool))
                parts = []
                if added:
                    parts.append("added " + ", ".join(added))
                if removed:
                    parts.append("removed " + ", ".join(removed))
                self.app.event_log.add("pool_change", "warning" if removed else "info",
                                    "Server pool %s -> now [%s]" % ("; ".join(parts), ", ".join(pool)),
                                    pool=pool, added=added, removed=removed)
            self._last_pool = pool

    # ================================================================ auto-scaling
    def _autoscale(self, now):
        primaries = [n for n in self.pool()]
        if not primaries:
            return
        self.avg_load = sum(self.app.stats.server_load(n) for n in primaries) / len(primaries)
        action = self.scaler.update(self.avg_load, now, self.standby_active)
        standby = [n for n in config.SERVERS if self.is_standby(n)]
        if action == "scale_up":
            self.standby_active = True
            healthy = [n for n in standby if self.state[n]["status"] == "healthy"]
            self.app.event_log.add("scale_up", "warning",
                                "Average load %.0f%% > %.0f%% for %.0fs: activating standby %s%s"
                                % (self.avg_load, config.SCALE_UP_LOAD, config.SCALE_UP_DURATION,
                                   ", ".join(standby), "" if healthy else " (not healthy yet)"),
                                avg_load=self.avg_load)
        elif action == "scale_down":
            self.standby_active = False
            self.app.event_log.add("scale_down", "info",
                                "Average load %.0f%% < %.0f%% for %.0fs: standby %s leaves the pool"
                                % (self.avg_load, config.SCALE_DOWN_LOAD, config.SCALE_DOWN_DURATION,
                                   ", ".join(standby)),
                                avg_load=self.avg_load)
