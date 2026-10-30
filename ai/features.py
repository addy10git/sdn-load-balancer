"""Feature definition shared by train.py and predictor.py.

Features for one link at time t:
    util_t-4, util_t-3, util_t-2, util_t-1, util_t   (last WINDOW utilisation %)
    flows_t                                          (active flow entries on the link)
Target: utilisation HORIZON_STEPS samples later (10 s ahead at a 2 s interval).
"""
import config

WINDOW = config.PREDICTION_WINDOW
HORIZON_STEPS = config.PREDICTION_HORIZON_STEPS
FEATURE_NAMES = ["util_t-%d" % i for i in range(WINDOW - 1, 0, -1)] + ["util_t", "flows"]


def feature_row(utils, flows):
    """Build one feature vector from the last WINDOW utilisation values."""
    window = list(utils)[-WINDOW:]
    if len(window) < WINDOW:
        window = [window[0] if window else 0.0] * (WINDOW - len(window)) + window
    return [float(u) for u in window] + [float(flows)]


def build_samples(utils, flows, window=WINDOW, horizon=HORIZON_STEPS):
    """Sliding-window samples from one contiguous series. Returns (X, y)."""
    X, y = [], []
    for t in range(window - 1, len(utils) - horizon):
        X.append([float(u) for u in utils[t - window + 1:t + 1]] + [float(flows[t])])
        y.append(float(utils[t + horizon]))
    return X, y
