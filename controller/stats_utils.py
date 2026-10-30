"""Counter -> rate conversion for OpenFlow port statistics (pure Python)."""


def rate_per_sec(prev, cur, dt):
    """Per-second rate of a monotonically increasing counter.

    A counter that went backwards (switch restart / port re-created) yields 0.
    """
    if dt <= 0 or cur < prev:
        return 0.0
    return (cur - prev) / float(dt)


def utilization_pct(bps, capacity_mbps):
    if capacity_mbps <= 0:
        return 0.0
    return max(0.0, min(100.0, bps / (capacity_mbps * 1e6) * 100.0))


class PortRateTracker(object):
    """Keeps the previous counters of every (dpid, port) and returns rates."""

    def __init__(self):
        self._prev = {}
        self.rates = {}

    def update(self, dpid, port, ts, tx_bytes, rx_bytes, tx_packets, rx_packets, drops):
        key = (dpid, port)
        cur = (ts, tx_bytes, rx_bytes, tx_packets, rx_packets, drops)
        prev = self._prev.get(key)
        self._prev[key] = cur
        if prev is None:
            return None
        dt = ts - prev[0]
        rates = {
            "tx_bps": rate_per_sec(prev[1], tx_bytes, dt) * 8,
            "rx_bps": rate_per_sec(prev[2], rx_bytes, dt) * 8,
            "tx_pps": rate_per_sec(prev[3], tx_packets, dt),
            "rx_pps": rate_per_sec(prev[4], rx_packets, dt),
            "drops": max(0, drops - prev[5]),
            "ts": ts,
        }
        self.rates[key] = rates
        return rates

    def get(self, dpid, port):
        return self.rates.get((dpid, port))

    def forget_dpid(self, dpid):
        for key in [k for k in self._prev if k[0] == dpid]:
            self._prev.pop(key, None)
            self.rates.pop(key, None)
