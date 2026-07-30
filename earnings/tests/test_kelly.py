"""Kelly sizing + classification math (earnlib.signal)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import pandas as pd
import pytest

from earnlib import signal as sg

CFG = {"signal_layer": {
    "classify": {"buy_min": 1.0, "short_max": -1.0},
    "conviction": {"calibration_quarters": 8, "min_calibration_events": 60},
    "kelly": {"lambda": 0.25, "payoff_b": "", "f_cap": 0.10,
              "fallback": "equal_weight"},
}}


def test_kelly_fixed_points():
    # even-money bet, p=0.6 -> full Kelly 0.2 -> quarter Kelly 0.05
    assert sg.kelly_fraction(0.6, 1.0, 0.25, 0.10) == pytest.approx(0.05)
    # edge exactly zero at p = 1/(b+1)
    assert sg.kelly_fraction(0.5, 1.0, 0.25, 0.10) == 0.0
    # below breakeven -> zero, never negative
    assert sg.kelly_fraction(0.4, 1.0, 0.25, 0.10) == 0.0


def test_kelly_cap_and_lambda_scaling():
    uncapped = sg.kelly_fraction(0.9, 2.0, 1.0, 10.0)
    assert sg.kelly_fraction(0.9, 2.0, 0.5, 10.0) == pytest.approx(uncapped / 2)
    assert sg.kelly_fraction(0.9, 2.0, 1.0, 0.10) == 0.10   # cap binds


def test_kelly_degenerate_inputs():
    assert sg.kelly_fraction(float("nan"), 1.0, 0.25, 0.1) == 0.0
    assert sg.kelly_fraction(0.7, 0.0, 0.25, 0.1) == 0.0
    assert sg.kelly_fraction(0.7, -1.0, 0.25, 0.1) == 0.0


def test_classify_strict_inequalities():
    assert sg.classify(1.01, CFG) == ("good", "buy")
    assert sg.classify(1.0, CFG) == ("neutral", "neutral")    # strict, matches incumbent
    assert sg.classify(-1.01, CFG) == ("bad", "short")
    assert sg.classify(-1.0, CFG) == ("neutral", "neutral")
    assert sg.classify(float("nan"), CFG) == ("neutral", "neutral")


def test_fit_and_decide_roundtrip():
    rng = np.random.default_rng(7)
    n = 400
    s = rng.uniform(1.0, 3.0, n)
    # win prob rises with |s|: p = 0.45 + 0.1 * (s - 1)
    wins = rng.uniform(size=n) < (0.45 + 0.10 * (s - 1))
    ret = np.where(wins, 0.01, -0.01)
    past = pd.DataFrame({"action": "buy", "s_ts": s, "raw_ret": ret})
    model = sg.fit_conviction(past, CFG)
    leg = model.leg("buy")
    assert leg is not None and leg.n == n
    assert leg.p_win(3.0) > leg.p_win(1.0)          # monotone in |s|
    d = sg.decide(2.5, model, CFG, n_calibration=n)
    assert d.action == "buy" and 0.0 <= d.kelly_f <= 0.10
    # below min calibration -> fallback sentinel
    d_fb = sg.decide(2.5, model, CFG, n_calibration=10)
    assert d_fb.kelly_f == -1.0 and d_fb.p_hat is None


def test_fit_conviction_insufficient_leg():
    past = pd.DataFrame({"action": ["buy"] * 5, "s_ts": [1.5] * 5,
                         "raw_ret": [0.01] * 5})
    model = sg.fit_conviction(past, CFG)
    assert model.leg("buy") is None and model.leg("short") is None
