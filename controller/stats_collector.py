"""Statistics collection (every STATS_INTERVAL seconds).

Each cycle:
  1. compute link/host-link utilisation from the previous cycle's replies,
  2. run the congestion predictor and update the routing weights,
  3. feed per-source packet rates and completed handshakes to the DDoS detector,
  4. persist to SQLite and append to the AI training CSV,
  5. send new OFPPortStatsRequest / OFPFlowStatsRequest to every switch.
"""
import collections
import csv
import logging
import os
import time

import config
from controller.path_utils import link_weight
from controller.stats_utils import PortRateTracker, utilization_pct

LOG = logging.getLogger("sdn.stats")


class StatsCollector(object):
    def __init__(self, app):
        self.app = app
        self.ports = PortRateTracker()
        self._flow_parts = collections.defaultdict(list)   # dpid -> partial multipart body
        self.flow_counts = {}          # cookie -> (ts, packet_count, byte_count) at the ingress
        self._prev_flow_counts = {}
        self.link_stats = {}           # link id -> latest metrics
        self.history = collections.defaultdict(
            lambda: collections.deque(maxlen=config.HISTORY_LENGTH))   # link -> (ts, util, mbps)
        self.total_history = collections.deque(maxlen=config.HISTORY_LENGTH)
        self.predictions = {}          # link id -> utilisation predicted for now + horizon
        self._pending_pred = collections.defaultdict(collections.deque)  # link -> (target_ts, value)
        self.pred_history = collections.defaultdict(
            lambda: collections.deque(maxlen=config.HISTORY_LENGTH))   # link -> (ts, actual, predicted)
        self.total_mbps = 0.0
        self.last_cycle = None
        self._csv_ready = False

    # ================================================================ requests / replies
    def request_stats(self):
        for dp in list(self.app.datapaths.values()):
            ofp, parser = dp.ofproto, dp.ofproto_parser
            dp.send_msg(parser.OFPPortStatsRequest(dp, 0, ofp.OFPP_ANY))
            dp.send_msg(parser.OFPFlowStatsRequest(dp))

    def on_port_stats(self, msg):
        now = time.time()
        dpid = msg.datapath.id
        for st in msg.body:
            self.ports.update(dpid, st.port_no, now, st.tx_bytes, st.rx_bytes,
                              st.tx_packets, st.rx_packets, st.tx_dropped + st.rx_dropped)

    def on_flow_stats(self, msg):
        dpid = msg.datapath.id
        self._flow_parts[dpid].extend(msg.body)
        if msg.flags & msg.datapath.ofproto.OFPMPF_REPLY_MORE:
            return
        body = self._flow_parts.pop(dpid, [])
        now = time.time()
        records = self.app.routing.records
        for st in body:
            record = records.get(st.cookie)
            if record is not None and record.ingress == dpid:
                record.packets, record.bytes = st.packet_count, st.byte_count
                self.flow_counts[st.cookie] = (now, st.packet_count, st.byte_count)

    # ================================================================ accessors
    @staticmethod
    def host_link_id(dpid, host_name):
        return "s%d-%s" % (dpid, host_name)

    def server_link_id(self, server):
        host = self.app.topo.host_by_ip(config.SERVERS[server]["ip"])
        return self.host_link_id(host["dpid"], server) if host else None

    def link_util(self, link_id):
        stats = self.link_stats.get(link_id)
        return stats["util"] if stats else 0.0

    def server_load(self, server):
        return self.link_util(self.server_link_id(server))

    # ================================================================ main cycle
    def run_cycle(self):
        now = time.time()
        try:
            self._compute(now)
        except Exception:  # never let a bad sample kill the polling loop
            LOG.exception("stats computation failed")
        self.request_stats()
        self.last_cycle = now

    def _compute(self, now):
        topo = self.app.topo
        flows_per_edge = self.app.routing.flows_per_edge()
        current = {}

        # --- switch-to-switch links (per-direction max of the two egress ports)
        for a, b, data in topo.links():
            ports = data["ports"]
            ra, rb = self.ports.get(a, ports[a]), self.ports.get(b, ports[b])
            if ra is None and rb is None:
                continue
            bps = max(ra["tx_bps"] if ra else 0.0, rb["tx_bps"] if rb else 0.0)
            drops = (ra["drops"] if ra else 0) + (rb["drops"] if rb else 0)
            current[topo.link_id(a, b)] = {
                "type": "switch", "source": "s%d" % a, "target": "s%d" % b,
                "util": utilization_pct(bps, config.LINK_BW_MBPS), "mbps": bps / 1e6,
                "drops": drops, "flows": flows_per_edge.get((a, b), 0),
            }

        # --- host access links
        active_by_server = self.app.lb.active_flows()
        for mac, host in list(topo.hosts.items()):
            rates = self.ports.get(host["dpid"], host["port"])
            if rates is None:
                continue
            name = config.host_name_by_ip(host.get("ip")) or mac
            bps = max(rates["tx_bps"], rates["rx_bps"])
            link = self.host_link_id(host["dpid"], name)
            current[link] = {
                "type": "host", "source": "s%d" % host["dpid"], "target": name,
                "util": utilization_pct(bps, config.HOST_LINK_BW_MBPS), "mbps": bps / 1e6,
                "drops": rates["drops"], "flows": active_by_server.get(name, 0),
            }

        if not current:
            return

        # --- history, predictions, prediction accuracy
        predictor = self.app.predictor
        pred_rows, horizon = [], config.PREDICTION_HORIZON_SECONDS
        for link, st in current.items():
            self.history[link].append((now, st["util"], st["mbps"]))
            pending = self._pending_pred[link]
            while pending and pending[0][0] <= now + config.STATS_INTERVAL / 2.0:
                target_ts, predicted = pending.popleft()
                self.pred_history[link].append((now, st["util"], predicted))
                self.app.store.fill_prediction_actual(link, target_ts, st["util"])
            utils = [u for _, u, _ in self.history[link]]
            predicted = predictor.predict(utils, st["flows"])
            st["predicted"] = predicted
            self.predictions[link] = predicted
            pending.append((now + horizon, predicted))
            pred_rows.append((now, link, predicted, now + horizon, None))
        self.link_stats = current
        for link in [l for l in self.predictions if l not in current]:
            self.predictions.pop(link, None)

        # --- routing weights: 1 + utilisation cost + predicted-congestion penalty
        for a, b, _ in topo.links():
            link = topo.link_id(a, b)
            st = current.get(link, {})
            topo.set_weight(a, b, link_weight(
                st.get("util", 0.0), st.get("predicted"), config.UTIL_COST_WEIGHT,
                config.PREDICTION_CONGESTION_THRESHOLD, config.PREDICTION_PENALTY))

        # --- totals (traffic served = sum of server access links)
        self.total_mbps = sum(st["mbps"] for st in current.values()
                              if st["type"] == "host" and st["target"] in config.SERVERS)
        self.total_history.append((now, self.total_mbps))

        self._feed_security(now)
        self._persist(now, current, pred_rows)

    def _feed_security(self, now):
        """Per-source packet rate and completed handshakes from ingress counters."""
        pps_by_src = collections.defaultdict(float)
        established = collections.Counter()
        records = self.app.routing.records
        for cookie, (ts, packets, _bytes) in list(self.flow_counts.items()):
            record = records.get(cookie)
            if record is None:
                self.flow_counts.pop(cookie, None)
                self._prev_flow_counts.pop(cookie, None)
                continue
            if record.kind == "lb_rev":
                continue  # server -> client direction is not a client source
            prev = self._prev_flow_counts.get(cookie)
            if prev is not None and ts > prev[0] and packets >= prev[1]:
                pps_by_src[record.src_ip] += (packets - prev[1]) / (ts - prev[0])
            self._prev_flow_counts[cookie] = (ts, packets)
            if record.kind == "lb_fwd" and packets >= 3:
                session = self.app.lb.sessions.get(record.session_key)
                if session is not None and not session.established and session.key[1] == 6:
                    session.established = True
                    established[record.src_ip] += 1
        self.app.security.on_stats(dict(pps_by_src), established, now)

    def _persist(self, now, current, pred_rows):
        store = self.app.store
        rows = [(now, link, round(st["util"], 3), round(st["mbps"], 4), st["drops"], st["flows"])
                for link, st in current.items()]
        try:
            store.add_link_stats(rows)
            store.add_predictions(pred_rows)
            health = self.app.health
            flows = self.app.lb.active_flows()
            store.add_server_stats([(now, name, health.status(name), int(health.in_pool(name)),
                                     round(self.server_load(name), 3), flows.get(name, 0))
                                    for name in config.SERVERS])
            store.commit()
        except Exception as exc:
            LOG.warning("could not persist stats: %s", exc)
        if config.LOG_STATS_CSV:
            self._append_csv(rows)

    def _append_csv(self, rows):
        try:
            os.makedirs(os.path.dirname(config.STATS_CSV), exist_ok=True)
            new_file = not os.path.exists(config.STATS_CSV)
            with open(config.STATS_CSV, "a", newline="") as fh:
                writer = csv.writer(fh)
                if new_file:
                    writer.writerow(["ts", "link", "util", "mbps", "drops", "flows"])
                writer.writerows(rows)
        except OSError as exc:
            LOG.warning("could not append training CSV: %s", exc)
