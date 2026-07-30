"""Native derivations from XBRL inputs — EBITDA, tangible book, shares, FCF, equity alias,
and the annual-filing duration fallback. All offline (no network)."""
from __future__ import annotations

import pytest

from src.coverage.fundamentals import (
    _derive_native,
    _is_annual_label,
    _latest_duration_facts,
    _period_label,
)
from src.coverage.native import _reconcile_share_scale


def test_period_label_strips_full_extension():
    # Regression: gzip-compressed filings must not leave a '.json' tail on the period label, or annual
    # DURATION facts (net income / revenue / eps) fail to period-match and drop to None.
    assert _period_label("GBM_2025-FY.json.gz") == "2025-FY"
    assert _period_label("GMEXICO_2026-1T.json") == "2026-1T"
    assert _period_label("FINDEP_2024-FY.json.gz") == "2024-FY"
    assert ".json" not in _period_label("INVEX_2025-FY.json.gz")


def test_share_scale_corrects_thousands_artifact():
    """Simec: direct XBRL count 497,709mn is a ×1000 'thousands' artifact; the ni/eps count 497.3mn
    corroborates and rescaling restores a sane P/S — so it is corrected to ~497.7mn."""
    out = _reconcile_share_scale(497709.214, 497.3, price=174.0, revenue=30540.322)
    assert out == pytest.approx(497.709, rel=1e-3)


def test_share_scale_leaves_healthy_count_untouched():
    """A count already yielding a sane P/S is never inspected or rescaled."""
    assert _reconcile_share_scale(500.0, 498.0, price=100.0, revenue=40000.0) == 500.0


def test_share_scale_corrects_impossible_magnitude_without_corroborator():
    """Club America: a loss-maker (no usable ni/eps) whose direct count is an impossible 340,621mn
    (340bn). The magnitude itself proves the count — not the price — is broken, so a ÷1000 rescale to
    ~340.6mn that lands a sane P/S is applied even without an ni/eps corroborator."""
    out = _reconcile_share_scale(340621.798257, None, price=81.9, revenue=6550.392)
    assert out == pytest.approx(340.622, rel=1e-3)


def test_share_scale_requires_sane_ps_after_rescale():
    # Missing price → cannot validate P/S → unchanged (never rescale blind).
    assert _reconcile_share_scale(497709.0, 497.3, price=None, revenue=30540.0) == 497709.0


def test_share_scale_does_not_touch_price_broken_name():
    """Vasconia's shares (96.7mn) are fine; the defect is a wrong price (0.32). With no ni/eps
    corroborator for a rescale, the count must be left alone rather than inflated to mask the price."""
    assert _reconcile_share_scale(96.72, None, price=0.32, revenue=2390.9) == 96.72


def test_derive_ebitda_tangible_shares_fcf():
    m = {"operating_income": 100.0, "depreciation": 30.0, "equity": 500.0,
         "intangibles": 40.0, "goodwill": 60.0, "net_income": 80.0, "eps": 2.0,
         "cfo": 120.0, "capex": 45.0}
    _derive_native(m)
    assert m["ebitda"] == pytest.approx(130.0)            # 100 + 30
    assert m["total_equity"] == 500.0                     # equity → total_equity alias
    assert m["tangible_book"] == pytest.approx(400.0)     # 500 - 40 - 60
    assert m["shares_out"] == pytest.approx(40.0)         # 80 / 2
    assert m["fcf"] == pytest.approx(75.0)                # 120 - 45


def test_derive_is_gap_tolerant_and_non_destructive():
    # missing depreciation → no ebitda; existing ebitda preserved
    m = {"operating_income": 100.0, "ebitda": 999.0, "equity": 200.0}
    _derive_native(m)
    assert m["ebitda"] == 999.0                            # not overwritten
    assert m["tangible_book"] == 200.0                     # equity, no intangibles/goodwill
    assert "shares_out" not in m                           # no eps → no shares
    m2 = {"net_income": 50.0, "eps": 0.0}                  # eps 0 → no divide-by-zero
    _derive_native(m2)
    assert "shares_out" not in m2


def test_annual_label_detection():
    assert _is_annual_label("2025-FY")
    assert _is_annual_label("2024")
    assert not _is_annual_label("2025-1T")


def test_latest_duration_fallback_scales_currency_not_eps():
    # a bank-style annual filing: income facts are prior-FY durations; pick the latest, scale to mn
    facts = {
        "ifrs-full_ProfitLoss": [
            {"value": 52e9, "period_end": "2023-12-31", "instant": None},
            {"value": 56e9, "period_end": "2024-12-31", "instant": None},
        ],
        "ifrs-full_BasicEarningsLossPerShare": [
            {"value": 18.3, "period_end": "2023-12-31", "instant": None},
            {"value": 19.7, "period_end": "2024-12-31", "instant": None},
        ],
    }
    out = _latest_duration_facts(facts, ["net_income", "eps"], ppu=1e6)
    assert out["net_income"] == pytest.approx(56_000.0)   # 56e9 / 1e6, latest year
    assert out["eps"] == pytest.approx(19.7)              # per-share, unscaled
