"""Direction x magnitude sizing mechanics (simulate_direction_magnitude)."""
from __future__ import annotations

import sys
from pathlib import Path

EARNINGS_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EARNINGS_ROOT))
sys.path.insert(0, str(EARNINGS_ROOT / "scripts"))

import numpy as np
import pandas as pd
import pytest

from simulate_direction_magnitude import (F_CAP, MIN_CLASS, day_returns,
                                          fit_class_kelly, size_weights)

CFG = {"signal_layer": {"kelly": {"lambda": 0.25}}}


def test_size_weights_caps_and_saturation():
    df = pd.DataFrame({"s_ts": [0.5, 2.0, 4.0], "sigma_pre": [0.015, 0.03, 0.09]})
    m = size_weights(df, "model")
    assert m == pytest.approx([F_CAP * 0.25, F_CAP, F_CAP])   # saturates at S_REF
    v = size_weights(df, "vol")
    assert v == pytest.approx([F_CAP * 0.5, F_CAP, F_CAP])    # saturates at SIG_REF
    assert size_weights(df, "equal") == pytest.approx([F_CAP] * 3)


def test_day_returns_direction_is_analyst_and_idle_capital():
    # one day, two trades: analyst long a faller, analyst short a riser
    d = pd.DataFrame({"t0": ["2025-01-02"] * 2, "direction": [1, -1],
                      "raw_ret": [-0.02, -0.03], "s_ts": [3.0, -3.0],
                      "sigma_pre": [0.09, 0.09], "strength": [1.0, 1.0]})
    r = day_returns(d, "equal", 0)
    # long loses 2%, short gains 3%; each at F_CAP; rest of capital idle
    assert float(r.iloc[0]) == pytest.approx(F_CAP * (-0.02) + F_CAP * 0.03)
    # gross normalization: 15 trades at 0.10 -> gross 1.5 -> weights sum to 1
    big = pd.DataFrame({"t0": ["2025-01-02"] * 15, "direction": [1] * 15,
                        "raw_ret": [0.01] * 15, "s_ts": [3.0] * 15,
                        "sigma_pre": [0.09] * 15, "strength": [1.0] * 15})
    rb = day_returns(big, "equal", 0)
    assert float(rb.iloc[0]) == pytest.approx(0.01)           # fully invested


def test_fit_class_kelly_pit_fallback():
    # 12 fuerte trades in early quarters, none for suave -> suave stays F_CAP;
    # fuerte calibrates only once trailing window has >= MIN_CLASS trades
    rows = []
    quarters = [f"2024-{q}T" for q in (1, 2, 3)] + ["2025-1T"]
    for qi, q in enumerate(quarters[:3]):
        for i in range(4):
            rows.append({"period": q, "strength": 1.0, "direction": 1,
                         "raw_ret": 0.02 if i % 4 else -0.01})
    rows.append({"period": "2025-1T", "strength": 1.0, "direction": 1,
                 "raw_ret": 0.02})
    rows.append({"period": "2025-1T", "strength": 0.5, "direction": -1,
                 "raw_ret": 0.02})
    d = pd.DataFrame(rows)
    out = fit_class_kelly(d, CFG)
    last = out[out["period"] == "2025-1T"]
    fuerte = last[last["strength"] == 1.0]["kelly_f"].iloc[0]
    suave = last[last["strength"] == 0.5]["kelly_f"].iloc[0]
    assert suave == F_CAP                       # never calibrated -> fallback
    assert 0.0 <= fuerte <= F_CAP               # calibrated (12 >= MIN_CLASS)
    # early quarter has < MIN_CLASS trailing -> fallback
    first = out[out["period"] == "2024-1T"]["kelly_f"]
    assert (first == F_CAP).all()
    assert MIN_CLASS == 10
