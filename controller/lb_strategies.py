"""Server selection strategies for the load balancer (pure Python).

Every strategy gets the list of candidate server names (healthy and in the pool)
and a SelectionContext with the live measurements, and returns one name.
"""


class SelectionContext(object):
    """Measurements a strategy may use. All dicts are keyed by server name."""

    def __init__(self, server_load=None, active_flows=None, predicted_load=None,
                 path_congestion=None, congestion_threshold=80.0):
        self.server_load = server_load or {}          # current access-link load %
        self.active_flows = active_flows or {}        # live connections
        self.predicted_load = predicted_load or {}    # AI-predicted access-link load %
        self.path_congestion = path_congestion or {}  # max predicted % on the client->server path
        self.congestion_threshold = congestion_threshold


class RoundRobinStrategy(object):
    name = "round_robin"

    def __init__(self):
        self._next = 0

    def select(self, candidates, ctx):
        if not candidates:
            return None
        ordered = sorted(candidates)
        choice = ordered[self._next % len(ordered)]
        self._next += 1
        return choice


class LeastLoadStrategy(object):
    """Lowest current traffic on the server's access link; ties -> fewest flows."""
    name = "least_load"

    def select(self, candidates, ctx):
        if not candidates:
            return None
        return min(sorted(candidates), key=lambda s: (
            round(ctx.server_load.get(s, 0.0), 1), ctx.active_flows.get(s, 0)))


class AIPredictiveStrategy(object):
    """Avoid servers and paths the predictor expects to be congested.

    score = 0.6 * predicted server load + 0.4 * worst predicted link on the path,
    plus a large penalty if either is above the congestion threshold. Without
    predictions it behaves like least_load (predictions default to current load).
    """
    name = "ai_predictive"
    PENALTY = 100.0

    def score(self, server, ctx):
        server_pred = ctx.predicted_load.get(server, ctx.server_load.get(server, 0.0))
        path_pred = ctx.path_congestion.get(server, 0.0)
        score = 0.6 * server_pred + 0.4 * path_pred
        if server_pred > ctx.congestion_threshold or path_pred > ctx.congestion_threshold:
            score += self.PENALTY
        return score

    def select(self, candidates, ctx):
        if not candidates:
            return None
        return min(sorted(candidates), key=lambda s: (
            round(self.score(s, ctx), 1), ctx.active_flows.get(s, 0)))


STRATEGIES = {
    RoundRobinStrategy.name: RoundRobinStrategy,
    LeastLoadStrategy.name: LeastLoadStrategy,
    AIPredictiveStrategy.name: AIPredictiveStrategy,
}


def make_strategy(name):
    if name not in STRATEGIES:
        raise ValueError("unknown strategy %r (choose from %s)" % (name, ", ".join(STRATEGIES)))
    return STRATEGIES[name]()
