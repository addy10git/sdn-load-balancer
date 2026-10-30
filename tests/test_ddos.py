"""DDoS threshold logic and the auto-scaling hysteresis."""
from controller.autoscaler import AutoScaler
from controller.ddos_detector import DdosDetector

SRC = "10.0.0.4"


def make():
    return DdosDetector(pps_threshold=1000, new_flow_threshold=40, rate_window=2.0,
                        syn_ack_ratio=3.0, syn_min_count=20, syn_window=10.0)


def test_normal_client_not_flagged():
    d = make()
    for i in range(20):  # 10 new flows per second
        d.record_new_flow(SRC, 100 + i * 0.1, is_syn=True)
        d.record_established(SRC, 100 + i * 0.1)
    d.update_pps(SRC, 300)
    assert d.evaluate(SRC, 102.0) == []


def test_new_flow_rate_triggers():
    d = make()
    for i in range(100):  # 100 flows in one second
        d.record_new_flow(SRC, 50 + i * 0.01)
    reasons = d.evaluate(SRC, 51.0)
    assert any("new flows" in r for r in reasons)


def test_new_flow_window_expires():
    d = make()
    for i in range(100):
        d.record_new_flow(SRC, 50 + i * 0.01)
    assert d.evaluate(SRC, 60.0) == []


def test_packet_rate_triggers():
    d = make()
    d.update_pps(SRC, 5000)
    assert any("packet rate" in r for r in d.evaluate(SRC, 1.0))


def test_syn_ack_ratio_triggers_only_with_enough_syns():
    d = make()
    for i in range(10):
        d.record_new_flow(SRC, 1 + i * 0.5, is_syn=True)
    assert d.evaluate(SRC, 6.0) == []          # below syn_min_count
    for i in range(20):
        d.record_new_flow(SRC, 6 + i * 0.1, is_syn=True)
    d.record_established(SRC, 7.0, count=2)
    reasons = d.evaluate(SRC, 8.5)
    assert any("SYN/ACK" in r for r in reasons)


def test_balanced_handshakes_not_flagged():
    d = make()
    for i in range(30):
        d.record_new_flow(SRC, 1 + i * 0.3, is_syn=True)
        d.record_established(SRC, 1 + i * 0.3)
    assert not any("SYN/ACK" in r for r in d.evaluate(SRC, 10.0))


def test_check_all_and_reset():
    d = make()
    d.update_pps(SRC, 5000)
    d.update_pps("10.0.0.1", 10)
    assert set(d.check_all(1.0)) == {SRC}
    d.reset(SRC)
    assert d.check_all(1.0) == {}


def test_autoscaler_hysteresis():
    s = AutoScaler(up_load=75, up_duration=15, down_load=30, down_duration=30)
    assert s.update(90, 0, False) is None
    assert s.update(90, 10, False) is None
    assert s.update(90, 15, False) == "scale_up"
    # load dips below the threshold -> timer restarts
    s2 = AutoScaler(75, 15, 30, 30)
    s2.update(90, 0, False)
    s2.update(50, 8, False)
    assert s2.update(90, 16, False) is None
    # scale down needs 30 s below 30 %
    assert s.update(20, 100, True) is None
    assert s.update(20, 120, True) is None
    assert s.update(20, 130, True) == "scale_down"
