"""Link-failure handling and recovery-time measurement.

On a link failure (PortStatus link-down or an LLDP link-delete, whichever comes
first) we:
  1. remove the link from the graph,
  2. delete every installed path that crossed it (by cookie),
  3. recompute and install new paths for those flows,
  4. send an OFPBarrierRequest to every switch that received new rules.
Recovery time = failure detected -> last BarrierReply (all new rules installed).
"""
import collections
import logging
import time

LOG = logging.getLogger("sdn.fault")


class FaultTolerance(object):
    def __init__(self, app):
        self.app = app
        self._waiting = {}           # xid -> recovery id
        self._recoveries = {}        # recovery id -> info
        self._next_id = 1
        self.history = collections.deque(maxlen=50)
        self.last_recovery_ms = None

    def link_down(self, a, b, cause):
        t0 = time.time()
        topo, routing = self.app.topo, self.app.routing
        if not topo.remove_link(a, b):
            return  # already handled (PortStatus and LLDP both report the same failure)
        affected = routing.records_using_link(a, b)
        link = topo.link_id(a, b)
        self.app.event_log.add("link_down", "error", "Link %s down (%s), %d flow path(s) affected"
                            % (link, cause, len(affected)), link=link)
        if not affected:
            return

        sessions, routes = set(), set()
        for record in affected:
            routing.remove_record(record.cookie)
            if record.session_key is not None:
                sessions.add(record.session_key)
            else:
                routes.add((record.src_ip, record.dst_ip, record.ingress))

        touched = set()
        rerouted = 0
        for key in sessions:
            dpids = self.app.lb.reinstall_session(key)
            touched |= dpids
            rerouted += 1 if dpids else 0
        for src_ip, dst_ip, ingress in routes:
            record = routing.install_route(src_ip, dst_ip, ingress)
            if record is not None:
                touched |= record.dpids()
                rerouted += 1

        rid = self._next_id
        self._next_id += 1
        info = {"id": rid, "link": link, "t0": t0, "flows": len(affected), "rerouted": rerouted,
                "pending": set()}
        self._recoveries[rid] = info
        for dpid in touched:
            dp = self.app.datapaths.get(dpid)
            if dp is None:
                continue
            req = dp.ofproto_parser.OFPBarrierRequest(dp)
            dp.send_msg(req)
            info["pending"].add(req.xid)
            self._waiting[req.xid] = rid
        if not info["pending"]:
            self._finish(rid)

    def on_barrier_reply(self, xid):
        rid = self._waiting.pop(xid, None)
        if rid is None or rid not in self._recoveries:
            return
        info = self._recoveries[rid]
        info["pending"].discard(xid)
        if not info["pending"]:
            self._finish(rid)

    def _finish(self, rid):
        info = self._recoveries.pop(rid)
        ms = (time.time() - info["t0"]) * 1000.0
        self.last_recovery_ms = round(ms, 2)
        entry = {"ts": time.time(), "link": info["link"], "recovery_ms": self.last_recovery_ms,
                 "flows": info["flows"], "rerouted": info["rerouted"]}
        self.history.append(entry)
        self.app.event_log.add("recovery", "success",
                            "Rerouted %d/%d paths around %s in %.1f ms"
                            % (info["rerouted"], info["flows"], info["link"], ms), **entry)

    def link_up(self, a, b):
        self.app.event_log.add("link_up", "success", "Link %s is up" % self.app.topo.link_id(a, b),
                            link=self.app.topo.link_id(a, b))
