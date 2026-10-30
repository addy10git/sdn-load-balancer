"""iperf/ping output parsing (control API) and the SQLite store."""
import io

from common.db import Store
from control_api import parse_iperf, parse_ping
from controller.stats_utils import PortRateTracker, rate_per_sec, utilization_pct

IPERF_TCP = """------------------------------------------------------------
Client connecting to 10.0.0.100, TCP port 5001
[  3] local 10.0.0.1 port 40000 connected with 10.0.0.100 port 5001
[ ID] Interval       Transfer     Bandwidth
[  3]  0.0-10.0 sec  11.2 MBytes  9.41 Mbits/sec
"""
IPERF_UDP = """[  3]  0.0-10.0 sec  1.19 MBytes  1.00 Mbits/sec
[  3] Sent 852 datagrams
[  3] Server Report:
[  3]  0.0-10.0 sec  1.18 MBytes  0.99 Mbits/sec   0.412 ms    5/  852 (0.59%)
"""
PING = """20 packets transmitted, 19 received, 5% packet loss, time 3805ms
rtt min/avg/max/mdev = 22.101/24.553/31.002/1.874 ms
"""


def test_parse_iperf_tcp():
    r = parse_iperf(IPERF_TCP)
    assert r["throughput_mbps"] == 9.41 and r["jitter_ms"] is None


def test_parse_iperf_udp():
    r = parse_iperf(IPERF_UDP, udp=True)
    assert r["throughput_mbps"] == 0.99
    assert r["jitter_ms"] == 0.412 and r["loss_pct"] == 0.59


def test_parse_ping():
    r = parse_ping(PING)
    assert r == {"avg_ms": 24.553, "mdev_ms": 1.874, "loss_pct": 5.0}


def test_rates_and_utilisation():
    assert rate_per_sec(100, 300, 2) == 100
    assert rate_per_sec(300, 100, 2) == 0  # counter reset
    assert utilization_pct(5e6, 10) == 50.0
    assert utilization_pct(50e6, 10) == 100.0
    t = PortRateTracker()
    assert t.update(1, 1, 0.0, 0, 0, 0, 0, 0) is None
    r = t.update(1, 1, 2.0, 2_500_000, 0, 1000, 0, 4)
    assert r["tx_bps"] == 10_000_000 and r["tx_pps"] == 500 and r["drops"] == 4


def test_store_roundtrip(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    eid = store.add_event(1.0, "link_down", "error", "s3-s5 down", {"link": "s3-s5"})
    store.add_link_stats([(1.0, "s3-s5", 12.5, 1.25, 0, 2)])
    store.add_predictions([(1.0, "s3-s5", 20.0, 11.0, None)])
    store.fill_prediction_actual("s3-s5", 11.0, 18.0)
    store.commit()
    events = store.recent_events()
    assert events[0]["id"] == eid and events[0]["data"] == {"link": "s3-s5"}
    buf = io.StringIO()
    store.export_csv("predictions", buf)
    lines = buf.getvalue().strip().splitlines()
    assert lines[0] == "ts,link,predicted,target_ts,actual"
    assert lines[1].endswith("18.0")
