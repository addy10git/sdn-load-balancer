"""Server selection logic of the three load-balancing strategies."""
import pytest

from controller.lb_strategies import (AIPredictiveStrategy, LeastLoadStrategy, RoundRobinStrategy,
                                      SelectionContext, make_strategy)

SERVERS = ["srv1", "srv2", "srv3"]


def test_round_robin_cycles_in_order():
    rr = RoundRobinStrategy()
    picks = [rr.select(SERVERS, SelectionContext()) for _ in range(6)]
    assert picks == ["srv1", "srv2", "srv3", "srv1", "srv2", "srv3"]


def test_round_robin_skips_removed_server():
    rr = RoundRobinStrategy()
    picks = {rr.select(["srv1", "srv3"], SelectionContext()) for _ in range(4)}
    assert picks == {"srv1", "srv3"}


def test_least_load_picks_lowest_load():
    ctx = SelectionContext(server_load={"srv1": 70, "srv2": 20, "srv3": 45})
    assert LeastLoadStrategy().select(SERVERS, ctx) == "srv2"


def test_least_load_breaks_ties_by_active_flows():
    ctx = SelectionContext(server_load={"srv1": 0, "srv2": 0, "srv3": 0},
                           active_flows={"srv1": 3, "srv2": 1, "srv3": 2})
    assert LeastLoadStrategy().select(SERVERS, ctx) == "srv2"


def test_ai_predictive_avoids_predicted_congestion():
    # srv1 is idle now but predicted to be congested; srv2 is busier but stable.
    ctx = SelectionContext(server_load={"srv1": 10, "srv2": 40, "srv3": 60},
                           predicted_load={"srv1": 90, "srv2": 42, "srv3": 65})
    assert AIPredictiveStrategy().select(SERVERS, ctx) == "srv2"


def test_ai_predictive_avoids_congested_path():
    ctx = SelectionContext(predicted_load={"srv1": 10, "srv2": 20, "srv3": 30},
                           path_congestion={"srv1": 95, "srv2": 10, "srv3": 10})
    assert AIPredictiveStrategy().select(SERVERS, ctx) == "srv2"


def test_ai_predictive_falls_back_to_current_load():
    ctx = SelectionContext(server_load={"srv1": 50, "srv2": 5, "srv3": 30})
    assert AIPredictiveStrategy().select(SERVERS, ctx) == "srv2"


def test_no_candidates_returns_none():
    for name in ("round_robin", "least_load", "ai_predictive"):
        assert make_strategy(name).select([], SelectionContext()) is None


def test_unknown_strategy_rejected():
    with pytest.raises(ValueError):
        make_strategy("random")
