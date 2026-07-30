"""Reporting-currency auto-detection from XBRL facts — the authoritative signal that replaces the
frequently-wrong ``company.currency`` config value for USD reporters. All offline (no network)."""
from __future__ import annotations

from src.extract.xbrl_facts import (
    _USDMXN,
    detect_reporting_currency,
    pesos_per_unit_for,
)


def _entry(value, unit):
    return {"value": value, "instant": "2024-12-31", "period_start": None,
            "period_end": None, "dimensions": None, "unit": unit, "decimals": "-3"}


def test_detect_usd_dominant():
    facts = {
        "ifrs-full_Revenue": [_entry(4195e6, "ISO4217:USD")],
        "ifrs-full_ProfitLoss": [_entry(300e6, "ISO4217:USD")],
        "ifrs-full_Equity": [_entry(29000e6, "ISO4217:USD")],
    }
    assert detect_reporting_currency(facts) == "USD"


def test_detect_mxn_dominant():
    facts = {
        "ifrs-full_Revenue": [_entry(80000e6, "ISO4217:MXN")],
        "ifrs-full_Equity": [_entry(200000e6, "ISO4217:MXN")],
    }
    assert detect_reporting_currency(facts) == "MXN"


def test_lone_fx_note_usd_among_mxn_is_mxn():
    # An MXN filer with a single USD-tagged FX-note line item — the mode must stay MXN.
    facts = {
        "ifrs-full_Revenue": [_entry(80000e6, "ISO4217:MXN")],
        "ifrs-full_ProfitLoss": [_entry(6000e6, "ISO4217:MXN")],
        "some_fx_note": [_entry(12.0, "ISO4217:USD")],
    }
    assert detect_reporting_currency(facts) == "MXN"


def test_ignores_non_monetary_and_non_numeric_units():
    facts = {
        "ifrs_mx-cor_NumeroDeAccionesEnCirculacion": [_entry(1500e6, "xbrli:shares")],
        "ifrs-full_ratio": [_entry(0.31, "xbrli:pure")],
        "text": [_entry("n/a", "ISO4217:USD")],
    }
    assert detect_reporting_currency(facts) is None


def test_empty_or_none():
    assert detect_reporting_currency({}) is None
    assert detect_reporting_currency(None) is None


def test_detected_currency_overrides_config():
    # Config lies (says MXN) but the facts are USD → detected currency wins and applies ×USDMXN.
    cfg = {"company": {"unit": "millions", "currency": "MXN"}}
    assert pesos_per_unit_for(cfg, "USD") == 1e6 / _USDMXN
    assert pesos_per_unit_for(cfg, "MXN") == 1e6
    # No detected currency → falls back to config (here MXN).
    assert pesos_per_unit_for(cfg) == 1e6
    assert pesos_per_unit_for(cfg, None) == 1e6
