"""Rule-based DDoS detection (pure Python, no Ryu imports).

Three independent signals per source IP; any one of them flags the source:
  1. packets/second   measured from flow-stat counters at the source's edge switch
  2. new flows/second PacketIn events that created a new flow from the source
  3. SYN-to-ACK ratio TCP connection attempts vs. connections that completed the
                      handshake (seen as >= 3 packets on the connection's flow)
"""
import collections


class DdosDetector(object):
    def __init__(self, pps_threshold=1500, new_flow_threshold=40, rate_window=2.0,
                 syn_ack_ratio=3.0, syn_min_count=20, syn_window=10.0):
        self.pps_threshold = pps_threshold
        self.new_flow_threshold = new_flow_threshold
        self.rate_window = rate_window
        self.syn_ack_ratio = syn_ack_ratio
        self.syn_min_count = syn_min_count
        self.syn_window = syn_window
        self._new_flows = collections.defaultdict(collections.deque)   # src -> timestamps
        self._syns = collections.defaultdict(collections.deque)
        self._acks = collections.defaultdict(collections.deque)
        self._pps = {}

    # ------------------------------------------------------------- inputs
    def record_new_flow(self, src, now, is_syn=False):
        self._new_flows[src].append(now)
        if is_syn:
            self._syns[src].append(now)

    def record_established(self, src, now, count=1):
        for _ in range(count):
            self._acks[src].append(now)

    def update_pps(self, src, pps):
        self._pps[src] = float(pps)

    # ------------------------------------------------------------- metrics
    @staticmethod
    def _trim(dq, cutoff):
        while dq and dq[0] < cutoff:
            dq.popleft()

    def new_flow_rate(self, src, now):
        dq = self._new_flows.get(src)
        if not dq:
            return 0.0
        self._trim(dq, now - self.rate_window)
        return len(dq) / self.rate_window

    def syn_ack(self, src, now):
        """(syn_count, ack_count) within the SYN window."""
        cutoff = now - self.syn_window
        syns = self._syns.get(src, collections.deque())
        acks = self._acks.get(src, collections.deque())
        self._trim(syns, cutoff)
        self._trim(acks, cutoff)
        return len(syns), len(acks)

    def pps(self, src):
        return self._pps.get(src, 0.0)

    # ------------------------------------------------------------- decisions
    def evaluate(self, src, now):
        """Return a list of human-readable reasons; empty list means not an attack."""
        reasons = []
        pps = self.pps(src)
        if pps > self.pps_threshold:
            reasons.append("packet rate %.0f pps > %d" % (pps, self.pps_threshold))
        rate = self.new_flow_rate(src, now)
        if rate > self.new_flow_threshold:
            reasons.append("new flows %.0f/s > %d" % (rate, self.new_flow_threshold))
        syns, acks = self.syn_ack(src, now)
        if syns >= self.syn_min_count:
            ratio = syns / float(max(1, acks))
            if ratio > self.syn_ack_ratio:
                reasons.append("SYN/ACK ratio %.1f (%d SYN, %d ACK) > %.1f"
                               % (ratio, syns, acks, self.syn_ack_ratio))
        return reasons

    def check_all(self, now):
        sources = set(self._pps) | set(self._new_flows) | set(self._syns)
        found = {}
        for src in sources:
            reasons = self.evaluate(src, now)
            if reasons:
                found[src] = reasons
        return found

    def snapshot(self, now):
        """Current per-source metrics for the API."""
        sources = set(self._pps) | set(self._new_flows) | set(self._syns)
        out = {}
        for src in sorted(sources):
            syns, acks = self.syn_ack(src, now)
            out[src] = {"pps": round(self.pps(src), 1),
                        "new_flows_per_sec": round(self.new_flow_rate(src, now), 1),
                        "syn": syns, "ack": acks}
        return out

    def reset(self, src):
        for store in (self._new_flows, self._syns, self._acks, self._pps):
            store.pop(src, None)
