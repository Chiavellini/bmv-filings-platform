"""Analyst overlay join + scoring (earnlib.signal.analyst_overlay)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest

from earnlib import signal as sg
from earnlib import study

CFG = {"signal_layer": {
    "classify": {"buy_min": 1.0, "short_max": -1.0},
    "conviction": {"calibration_quarters": 8, "min_calibration_events": 60},
    "kelly": {"lambda": 0.25, "payoff_b": "", "f_cap": 0.10,
              "fallback": "equal_weight"},
}}

DEV_CFG = {
    "holdout": {"quarters": ["2025-4T"]},
    "phase_d": {"historical_holdout_quarters": ["2016-2T"]},
    "universe": {},
}


def _events(periods, s_ts):
    n = len(periods)
    return pd.DataFrame({
        "slug": [f"co{i}" for i in range(n)], "period": periods,
        "s_ts": s_ts, "s_cs": s_ts, "ar0_cc": [0.01] * n,
        "is_stale": [False] * n,
    })


def _pred(rows):
    return pd.DataFrame(rows, columns=["slug", "period", "direction",
                                       "strength"])


def test_overlay_formula():
    df = _events(["2025-1T", "2025-1T"], [0.4, -0.2])
    pred = _pred([("co0", "2025-1T", 1, 1.0), ("co1", "2025-1T", -1, 0.5)])
    for w in (0.25, 0.5):
        out = sg.analyst_overlay(df, pred, w)
        assert out["s_ts"].tolist() == pytest.approx(
            [0.4 + w * 1.0, -0.2 + w * -0.5])
        assert out["s_ts_base"].tolist() == pytest.approx([0.4, -0.2])


def test_no_call_unchanged():
    df = _events(["2025-1T", "2025-1T"], [0.9, 1.2])
    pred = _pred([("co0", "2025-1T", 1, 1.0)])
    out = sg.analyst_overlay(df, pred, 0.5)
    covered = out[out["slug"] == "co0"].iloc[0]
    uncovered = out[out["slug"] == "co1"].iloc[0]
    assert covered["covered"] and covered["s_ts"] == pytest.approx(1.4)
    assert not uncovered["covered"]
    assert uncovered["s_ts"] == uncovered["s_ts_base"] == pytest.approx(1.2)
    assert uncovered["analyst_signal"] == 0.0


def test_no_holdout_leak():
    df = _events(["2025-1T", "2025-4T", "2016-2T"], [0.5, 0.5, 0.5])
    dev = study.dev_sample(df, DEV_CFG)
    assert set(dev["period"]) == {"2025-1T"}
    # a prediction keyed to a holdout period joins nothing in the dev frame
    pred = _pred([("co1", "2025-4T", 1, 1.0), ("co2", "2016-2T", -1, 1.0)])
    out = sg.analyst_overlay(dev, pred, 0.5)
    assert not out["covered"].any()
    assert (out["s_ts"] == out["s_ts_base"]).all()


def test_flip_only_when_covered():
    df = _events(["2025-1T", "2025-1T"], [0.9, 0.9])
    pred = _pred([("co0", "2025-1T", 1, 1.0)])
    out = sg.analyst_overlay(df, pred, 0.25)
    actions = [sg.classify(s, CFG)[1] for s in out["s_ts"]]
    base_actions = [sg.classify(s, CFG)[1] for s in out["s_ts_base"]]
    assert base_actions == ["neutral", "neutral"]
    assert actions == ["buy", "neutral"]      # only the covered row flips


def test_zero_weight_identity():
    df = _events(["2025-1T", "2025-1T"], [1.5, -1.5])
    pred = _pred([("co0", "2025-1T", -1, 1.0), ("co1", "2025-1T", 1, 0.5)])
    out = sg.analyst_overlay(df, pred, 0.0)
    assert (out["s_ts"] == out["s_ts_base"]).all()
    assert out["covered"].all()
