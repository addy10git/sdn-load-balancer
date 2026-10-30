"""Predictor fallback behaviour, feature construction and the training pipeline."""
import csv
import math
import os
import random

import pytest

from ai.features import FEATURE_NAMES, HORIZON_STEPS, WINDOW, build_samples, feature_row
from ai.predictor import CongestionPredictor, fallback_prediction


def test_missing_model_uses_fallback(tmp_path):
    p = CongestionPredictor(model_path=str(tmp_path / "none.joblib"))
    assert p.using_fallback
    assert p.model_name.startswith("fallback")


def test_corrupt_model_uses_fallback(tmp_path):
    bad = tmp_path / "bad.joblib"
    bad.write_bytes(b"not a model")
    p = CongestionPredictor(model_path=str(bad))
    assert p.using_fallback
    assert p.predict([10, 10, 10, 10, 10]) == 10.0


def test_fallback_flat_series_predicts_itself():
    assert fallback_prediction([40, 40, 40, 40, 40]) == 40.0


def test_fallback_follows_trend_and_clips():
    assert fallback_prediction([10, 20, 30, 40, 50]) > 30.0
    assert fallback_prediction([50, 40, 30, 20, 10]) < 30.0
    assert fallback_prediction([90, 95, 99, 100, 100]) <= 100.0
    assert fallback_prediction([10, 5, 0, 0, 0]) >= 0.0


def test_predict_handles_short_and_empty_history(tmp_path):
    p = CongestionPredictor(model_path=str(tmp_path / "none.joblib"))
    assert p.predict([]) == 0.0
    assert p.predict([25.0]) == 25.0


def test_feature_row_pads_short_history():
    row = feature_row([5.0, 6.0], flows=3)
    assert len(row) == len(FEATURE_NAMES) == WINDOW + 1
    assert row[-1] == 3.0 and row[-2] == 6.0


def test_build_samples_alignment():
    utils = list(range(20))
    flows = [1] * 20
    X, y = build_samples(utils, flows)
    assert len(X) == 20 - (WINDOW - 1) - HORIZON_STEPS
    assert X[0][:WINDOW] == [0, 1, 2, 3, 4][:WINDOW]
    assert y[0] == utils[WINDOW - 1 + HORIZON_STEPS]


def test_training_pipeline_end_to_end(tmp_path):
    """Smoke test of train.py on a small synthetic fixture (code path only, not real results)."""
    pytest.importorskip("sklearn")
    from ai import train

    rng = random.Random(1)
    csv_path = tmp_path / "stats.csv"
    with open(csv_path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "link", "util", "mbps", "drops", "flows"])
        for link in ("s1-s3", "s3-s5"):
            for i in range(300):
                util = 50 + 40 * math.sin(i / 15.0) + rng.uniform(-3, 3)
                w.writerow([1000 + i * 2.0, link, round(util, 2), util / 10, 0, 2])
    model_out = tmp_path / "model.joblib"
    import sys
    argv = sys.argv
    sys.argv = ["train.py", "--csv", str(csv_path), "--model-out", str(model_out), "--no-report"]
    try:
        train.main()
    finally:
        sys.argv = argv
    assert os.path.exists(model_out)
    p = CongestionPredictor(model_path=str(model_out))
    assert not p.using_fallback
    value = p.predict([50, 55, 60, 65, 70], flows=2)
    assert 0.0 <= value <= 100.0
