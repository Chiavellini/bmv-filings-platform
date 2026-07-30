"""Regression tests for the residual-BROKEN correctness campaign: the honest fixes that turned the
last cluster of BROKEN coverage names into sane output or an honest INCOMPLETE. All offline.

Covers:
  * ar_pros_SerieNumberOfStocks share fallback (GFInbursa / Pena Verde / GNP annual filers)
  * per-filing millions-scale detection (Pena Verde FY2025 filed already in millions)
  * scale-corrupt live-price rejection (Vasconia's penny-adjusted Yahoo series)
  * placeholder residual pack — detection + no-override guard (Fibra Uptown scaffold)
  * prospectus-stub reclassification (GNP's all-zero ifrs-full tags → un-sourced)
"""
from __future__ import annotations

import pytest

from src.coverage.fundamentals import _is_prospectus_stub
from src.coverage.native import (
    _price_is_scale_corrupt,
    _serie_shares_total,
    merge_packs,
)
from src.bloomberg.ingest import detect_placeholder
from src.bloomberg.schema import BloombergPack
from src.extract.xbrl_facts import _facts_in_millions, pesos_per_unit_for, pesos_per_unit_for_facts


# --- ar_pros_SerieNumberOfStocks share fallback --------------------------------------------------

def _serie(value, end, member="O"):
    return {"value": value, "instant": end,
            "dimensions": [{"IdDimension": "ar_pros_SeriesTypedAxis",
                            "ElementoMiembroTipificado": member}]}


def test_serie_shares_single_series():
    """GFInbursa files one ordinary series (Serie O) as ar_pros_SerieNumberOfStocks → 6,667mn."""
    facts = {"ar_pros_SerieNumberOfStocks": [_serie(6_667_027_948.0, "2024-12-31")]}
    assert _serie_shares_total(facts) == pytest.approx(6_667_027_948.0)


def test_serie_shares_sums_across_series_at_latest_period():
    """A multi-series filer's total is the SUM across series at the latest period (older periods and
    other series must not double-count)."""
    facts = {"ar_pros_SerieNumberOfStocks": [
        _serie(100.0, "2023-12-31", "A"),          # stale period — ignored
        _serie(300.0, "2024-12-31", "A"),
        _serie(200.0, "2024-12-31", "B"),
    ]}
    assert _serie_shares_total(facts) == 500.0


def test_serie_shares_absent_returns_none():
    assert _serie_shares_total({}) is None
    assert _serie_shares_total({"ar_pros_SerieNumberOfStocks": []}) is None


# --- per-filing millions-scale detection ---------------------------------------------------------

def _monetary(v):
    return [{"value": v, "unit": "ISO4217:MXN"}]


def test_facts_in_millions_true_for_millions_denominated_filing():
    """Pena Verde FY2025: assets 48,054 / equity 6,962 are already in millions (not full pesos)."""
    facts = {"ifrs-full_Assets": _monetary(48_054.0), "ifrs-full_Equity": _monetary(6_962.0),
             "ifrs-full_Revenue": _monetary(25_230.3)}
    assert _facts_in_millions(facts) is True


def test_facts_in_millions_false_for_full_peso_filing():
    """A normal full-peso filer's totals are ~1e9-1e11 — never mistaken for millions."""
    facts = {"ifrs-full_Assets": _monetary(48_054_000_000.0),
             "ifrs-full_Equity": _monetary(4_202_237_768.0)}
    assert _facts_in_millions(facts) is False


def test_pesos_per_unit_downshifts_millions_filing_only():
    cfg = {"company": {"unit": "millions", "currency": "MXN"}}
    millions = {"ifrs-full_Equity": _monetary(6_962.0), "ifrs-full_Assets": _monetary(48_054.0)}
    full_peso = {"ifrs-full_Equity": _monetary(4_202_237_768.0)}
    # base config is 1e6 (full pesos → millions); the guard drops it to 1 only for the millions filing
    assert pesos_per_unit_for(cfg) == 1e6
    assert pesos_per_unit_for_facts(cfg, millions) == 1.0
    assert pesos_per_unit_for_facts(cfg, full_peso) == 1e6


def test_pesos_per_unit_leaves_thousands_config_untouched():
    """A thousands (miles) filer (base 1e3) is below the guard's 1e6 floor → never downshifted."""
    cfg = {"company": {"unit": "miles_mxn"}}
    tiny = {"ifrs-full_Equity": _monetary(5_000.0)}
    assert pesos_per_unit_for_facts(cfg, tiny) == 1e3


# --- scale-corrupt live-price rejection ----------------------------------------------------------

def test_price_scale_corrupt_rejects_penny_series():
    """Vasconia: a 0.32 price against a 12.25 XBRL book value (equity 1,185 / 96.7mn shares) implies
    P/B 0.026 — a Yahoo adjusted-close artifact, not a discount."""
    assert _price_is_scale_corrupt(0.32, 96.717889, 1184.988) is True


def test_price_scale_corrupt_passes_normal_and_deep_value():
    assert _price_is_scale_corrupt(50.0, 1000.0, 20000.0) is False   # P/B 2.5
    assert _price_is_scale_corrupt(5.0, 1000.0, 12500.0) is False    # P/B 0.4 — a real cheap stock


def test_price_scale_corrupt_needs_positive_equity_and_shares():
    assert _price_is_scale_corrupt(0.32, 96.7, None) is False
    assert _price_is_scale_corrupt(0.32, 96.7, 0) is False
    assert _price_is_scale_corrupt(0.32, None, 1184.988) is False
    assert _price_is_scale_corrupt(None, 96.7, 1184.988) is False


# --- placeholder residual pack: detection + no-override guard -------------------------------------

def test_detect_placeholder_flags_round_reit_scaffold():
    """Fibra Uptown's residual is hand-typed round numbers (ffo 14000, noi 24000, shares 3900…) —
    the peer-based tells miss a single-subject pack, so the round-whole-number rule must catch it."""
    pack = BloombergPack(slug="fibra_uptown")
    pack.subject.update({"shares_out": 3900, "net_debt": 100000, "ffo": 14000, "affo": 12000,
                         "noi": 24000, "ebitda_ltm": 24000, "nav_ps": 36, "occupancy": 94, "ltv": 33})
    suspect, reasons = detect_placeholder(pack)
    assert suspect is True
    assert any("round whole" in r for r in reasons)


def test_detect_placeholder_passes_real_precise_pack():
    """A pack carrying real cents/decimals is not a scaffold."""
    pack = BloombergPack(slug="acme")
    pack.subject.update({"shares_out": 3874.6, "net_debt": 98231.4, "ffo": 14102.7, "affo": 11987.3,
                         "noi": 23844.1, "nav_ps": 35.8})
    suspect, _ = detect_placeholder(pack)
    assert suspect is False


def test_merge_packs_placeholder_does_not_override_native_shares():
    """A suspected-placeholder residual must NOT clobber a real native XBRL share count (Fibra
    Uptown's 3,900mn dummy over a true 53mn CBFI count → P/S 367×)."""
    native = BloombergPack(slug="fibra_uptown")
    native.subject["shares_out"] = 53.44
    residual = BloombergPack(slug="fibra_uptown")
    residual.subject["shares_out"] = 3900.0
    residual.suspect_placeholder = True
    merged = merge_packs(native, residual)
    assert merged.subject["shares_out"] == 53.44


def test_merge_packs_trusted_residual_still_overrides_native_shares():
    """A trustworthy (non-placeholder) residual share count still overrides the weak native one."""
    native = BloombergPack(slug="acme")
    native.subject["shares_out"] = 100.0
    residual = BloombergPack(slug="acme")
    residual.subject["shares_out"] = 142.7
    merged = merge_packs(native, residual)
    assert merged.subject["shares_out"] == 142.7


# --- prospectus-stub reclassification ------------------------------------------------------------

def test_prospectus_stub_all_core_zero():
    """GNP: every ifrs-full core tag is 0/absent → an un-sourced prospectus stub (corpus pending)."""
    assert _is_prospectus_stub({"revenue": 0.0, "net_income": 0.0, "eps": 0.0}) is True
    assert _is_prospectus_stub({"revenue": 0, "net_income": None, "equity": None}) is True


def test_prospectus_stub_false_when_any_core_present():
    """A real operating company reports at least one nonzero core figure → never reclassified."""
    assert _is_prospectus_stub({"revenue": 25230.3, "net_income": 0.0}) is False
    assert _is_prospectus_stub({"revenue": 0.0, "total_equity": 6962.0}) is False
