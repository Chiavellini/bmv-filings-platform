"""Analyst label normalization is config-driven and strict."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EARNINGS_ROOT))


@pytest.fixture(scope="module")
def labels() -> dict:
    cfg = yaml.safe_load(open(EARNINGS_ROOT / "configs" / "analyst_labels.yaml"))
    return cfg


def test_all_five_labels_present(labels):
    assert set(labels["labels"]) == {"Positive", "Negative", "Neutral",
                                     "N to P", "N to N"}


def test_soft_calls_are_half_strength_directional(labels):
    lab = labels["labels"]
    assert lab["N to P"] == {"direction": 1, "strength": 0.5}
    assert lab["N to N"] == {"direction": -1, "strength": 0.5}
    assert lab["Positive"] == {"direction": 1, "strength": 1.0}
    assert lab["Negative"] == {"direction": -1, "strength": 1.0}
    assert lab["Neutral"]["direction"] == 0


def test_strict_mode_on(labels):
    assert labels["strict"] is True


def test_built_predictions_match_config(labels):
    out = EARNINGS_ROOT / "outputs" / "analyst_predictions.parquet"
    if not out.exists():
        pytest.skip("analyst_predictions.parquet not built")
    import pandas as pd

    df = pd.read_parquet(out)
    assert len(df) == 138
    assert set(df["label_raw"].unique()) <= set(labels["labels"])
    counts = df["label_raw"].value_counts().to_dict()
    assert counts == {"N to P": 37, "Positive": 33, "N to N": 25,
                      "Neutral": 22, "Negative": 21}
    # direction/strength columns are consistent with the config map
    for raw, spec in labels["labels"].items():
        sub = df[df["label_raw"] == raw]
        assert (sub["direction"] == spec["direction"]).all()
        assert (sub["strength"] == spec["strength"]).all()
