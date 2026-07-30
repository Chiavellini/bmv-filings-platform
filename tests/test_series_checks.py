"""Tests for the shared cell-suspect checks (series_checks)."""
from __future__ import annotations

import pandas as pd

from src.eval.series_checks import (
    IDENTITIES,
    compute_cell_suspects,
    identity_expected,
    magnitude_breaks,
)


def _series(vals, start_year=2020):
    out = []
    y, q = start_year, 1
    for v in vals:
        out.append((f"{y}-{q}T", float(v)))
        q += 1
        if q > 4:
            y, q = y + 1, 1
    return out


def test_sudden_low_break_is_flagged():
    # The requirement's exact failure: hundreds for 10 periods, then single-digit.
    breaks = magnitude_breaks(_series([800, 810, 805, 790, 820, 815, 800, 808, 812, 806, 4]))
    assert list(breaks) == ["2022-3T"]
    assert "magnitude outlier" in breaks["2022-3T"]


def test_sudden_high_break_is_flagged():
    breaks = magnitude_breaks(_series([800, 802, 805, 9999, 806, 808, 810, 812]))
    assert list(breaks) == ["2020-4T"]


def test_monotone_trend_is_not_flagged():
    # 10 → 100 over 12 quarters is a 10× secular trend; the global-median check
    # would flag both endpoints, the neighbor-window check must not.
    vals = [10, 13, 17, 22, 28, 35, 44, 55, 68, 82, 91, 100]
    assert magnitude_breaks(_series(vals)) == {}


def test_too_little_history_is_exempt():
    assert magnitude_breaks(_series([800, 810, 4])) == {}


def test_verified_cell_is_exempt_from_suspects():
    rows = [{"period": p, "revenue": v}
            for p, v in _series([800, 810, 805, 790, 820, 815, 800, 808, 812, 806, 4])]
    df = pd.DataFrame(rows)
    conf = {("2022-3T", "revenue"): {"confidence": 1.0, "flagged": False,
                                     "source": "[verified] manual"}}
    assert compute_cell_suspects(df, ["revenue"], conf=conf) == {}
    # without the pin the same cell is suspect
    suspects = compute_cell_suspects(df, ["revenue"], conf={})
    assert ("2022-3T", "revenue") in suspects


def test_sign_and_range_take_precedence_over_magnitude():
    rows = [{"period": p, "capex": v}
            for p, v in _series([100, 102, 98, 101, 99, 103, -900])]
    df = pd.DataFrame(rows)
    suspects = compute_cell_suspects(df, ["capex"])
    assert suspects[("2021-3T", "capex")] == "negative value (expected positive)"


def test_signed_metric_skips_sign_and_magnitude():
    rows = [{"period": p, "fx_gain_loss": v}
            for p, v in _series([5, -300, 2, 400, -1, 3, 250])]
    df = pd.DataFrame(rows)
    assert compute_cell_suspects(df, ["fx_gain_loss"]) == {}


def test_identity_table_evaluates():
    v = {"revenue": 100.0, "cogs": 60.0, "operating_income": 20.0,
         "depreciation": 5.0, "ebt": 18.0, "tax_expense": 6.0}
    by_target = {ident[0]: ident for ident in IDENTITIES}
    assert identity_expected(v, by_target["gross_profit"]) == 40.0
    assert identity_expected(v, by_target["ebitda"]) == 25.0
    assert identity_expected(v, by_target["net_income"]) == 12.0
