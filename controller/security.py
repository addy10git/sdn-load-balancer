"""DDoS mitigation: turns DdosDetector verdicts into drop rules at the edge.

A detected attacker gets a high-priority drop rule on its own access port
(match in_port + ipv4_src) with hard_timeout=DDOS_BLOCK_SECONDS, so the switch
unblocks it automatically. Servers and the controller's probe address are never
blocked.
"""
import logging
import time

import config
from controller.ddos_detector import DdosDetector
from controller.routing import ETH_TYPE_IP

LOG = logging.getLogger("sdn.security")
BLOCK_COOKIE = 0xDD05000000000000


class SecurityManager(object):
    def __init__(self, app):
        self.app = app
        self.detector = DdosDetector(
            pps_threshold=config.DDOS_PPS_THRESHOLD,
            new_flow_threshold=config.DDOS_NEW_FLOWS_PER_SEC,
            rate_window=config.DDOS_RATE_WINDOW,
            syn_ack_ratio=config.DDOS_SYN_ACK_RATIO,
            syn_min_count=config.DDOS_SYN_MIN_COUNT,
            syn_window=config.DDOS_SYN_WINDOW)
        self.blocked = {}            # ip -> {dpid, port, since, until, reasons}
        self.attacks_blocked = 0
        self.whitelist = {info["ip"] for info in config.SERVERS.values()} | {config.PROBE_IP, config.VIP}

    def is_blocked(self, ip):
        return ip in self.blocked

    # ================================================================ inputs
    def on_new_flow(self, src_ip, is_syn, now):
        """Called for every PacketIn that will create a new flow. True -> drop it."""
        if src_ip in self.whitelist:
            return False
        if src_ip in self.blocked:
            return True
        self.detector.record_new_flow(src_ip, now, is_syn)
        reasons = self.detector.evaluate(src_ip, now)
        if reasons:
            self.block(src_ip, reasons)
            return True
        return False

    def on_stats(self, pps_by_src, established, now):
        for src, pps in pps_by_src.items():
            if src not in self.whitelist:
                self.detector.update_pps(src, pps)
        for src, count in established.items():
            self.detector.record_established(src, now, count)
        for src, reasons in self.detector.check_all(now).items():
            if src not in self.whitelist and src not in self.blocked:
                self.block(src, reasons)
        self.expire(now)

    # ================================================================ actions
    def _block_match(self, parser, ip, port):
        return parser.OFPMatch(in_port=port, eth_type=ETH_TYPE_IP, ipv4_src=ip)

    def block(self, ip, reasons, duration=config.DDOS_BLOCK_SECONDS):
        host = self.app.topo.host_by_ip(ip)
        if host is None:
            LOG.warning("cannot block %s: location unknown", ip)
            return False
        dp = self.app.datapaths.get(host["dpid"])
        if dp is None:
            return False
        now = time.time()
        self.app.routing.add_flow(dp, config.PRIORITY_BLOCK,
                                  self._block_match(dp.ofproto_parser, ip, host["port"]),
                                  [], idle=0, hard=duration, cookie=BLOCK_COOKIE)
        self.blocked[ip] = {"ip": ip, "dpid": host["dpid"], "port": host["port"],
                            "since": now, "until": now + duration, "reasons": reasons,
                            "host": config.host_name_by_ip(ip) or ip}
        self.attacks_blocked += 1
        self.detector.reset(ip)
        self.app.event_log.add("attack_blocked", "critical",
                            "DDoS from %s (%s) blocked at s%d port %d for %ds: %s"
                            % (ip, config.host_name_by_ip(ip) or "?", host["dpid"], host["port"],
                               duration, "; ".join(reasons)),
                            ip=ip, reasons=reasons)
        return True

    def unblock(self, ip, reason="manual unblock"):
        entry = self.blocked.pop(ip, None)
        if entry is None:
            return False
        dp = self.app.datapaths.get(entry["dpid"])
        if dp is not None:
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPFlowMod(
                datapath=dp, command=ofp.OFPFC_DELETE_STRICT, priority=config.PRIORITY_BLOCK,
                table_id=0, out_port=ofp.OFPP_ANY, out_group=ofp.OFPG_ANY,
                match=self._block_match(parser, ip, entry["port"])))
        self.detector.reset(ip)
        self.app.event_log.add("attack_unblocked", "info", "%s unblocked (%s)" % (ip, reason), ip=ip)
        return True

    def expire(self, now):
        for ip, entry in list(self.blocked.items()):
            if entry["until"] <= now:
                # The switch already removed the rule (hard_timeout); just forget it.
                self.blocked.pop(ip, None)
                self.detector.reset(ip)
                self.app.event_log.add("attack_unblocked", "info",
                                    "%s automatically unblocked after %ds"
                                    % (ip, config.DDOS_BLOCK_SECONDS), ip=ip)

    # ================================================================ views
    def api_view(self, now=None):
        now = now or time.time()
        return {
            "blocked": [dict(e, remaining=max(0, round(e["until"] - now))) for e in self.blocked.values()],
            "attacks_blocked": self.attacks_blocked,
            "sources": self.detector.snapshot(now),
            "thresholds": {"pps": config.DDOS_PPS_THRESHOLD,
                           "new_flows_per_sec": config.DDOS_NEW_FLOWS_PER_SEC,
                           "syn_ack_ratio": config.DDOS_SYN_ACK_RATIO},
        }
