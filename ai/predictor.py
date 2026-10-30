"""Congestion predictor used by the controller every stats cycle.

If a trained model (ai/models/congestion_model.joblib, written by train.py)
exists it is used; otherwise a moving-average + damped-trend fallback runs, so
the controller works before any training.
"""
import logging
import os

import config
from ai.features import HORIZON_STEPS, WINDOW, feature_row

LOG = logging.getLogger("sdn.predictor")


def fallback_prediction(utils, window=WINDOW, horizon=HORIZON_STEPS):
    """Moving average of the last `window` samples plus half the recent trend.

    A flat series predicts itself; a rising series predicts a bit higher, a
    falling one a bit lower. Damping the trend by 0.5 avoids overshooting.
    """
    recent = [float(u) for u in list(utils)[-window:]]
    if not recent:
        return 0.0
    avg = sum(recent) / len(recent)
    slope = (recent[-1] - recent[0]) / (len(recent) - 1) if len(recent) > 1 else 0.0
    return max(0.0, min(100.0, avg + 0.5 * slope * horizon))


class CongestionPredictor(object):
    def __init__(self, model_path=config.MODEL_PATH):
        self.model = None
        self.model_name = "fallback-moving-average"
        self.metrics = None
        if model_path and os.path.exists(model_path):
            try:
                import joblib  # imported lazily: the fallback needs no ML libraries
                bundle = joblib.load(model_path)
                self.model = bundle["model"]
                self.model_name = bundle.get("name", "trained-model")
                self.metrics = bundle.get("metrics")
                LOG.info("loaded congestion model %s from %s", self.model_name, model_path)
            except Exception as exc:
                LOG.warning("could not load model %s (%s); using fallback", model_path, exc)
                self.model = None
        else:
            LOG.info("no trained model at %s; using moving-average fallback", model_path)

    @property
    def using_fallback(self):
        return self.model is None

    def predict(self, utils, flows=0):
        """Predicted utilisation (%) of one link PREDICTION_HORIZON_SECONDS ahead."""
        utils = list(utils)
        if not utils:
            return 0.0
        if self.model is None or len(utils) < WINDOW:
            return round(fallback_prediction(utils), 2)
        try:
            value = float(self.model.predict([feature_row(utils, flows)])[0])
        except Exception as exc:
            LOG.warning("model prediction failed (%s); using fallback", exc)
            value = fallback_prediction(utils)
        return round(max(0.0, min(100.0, value)), 2)
