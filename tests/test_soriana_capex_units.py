"""Regression coverage for Soriana quarterly-capex unit handling.

Quarterly capex is now recovered by the deterministic custom extractor
(`extract_soriana_capex`), tier-pinned via `capex: [qcapex]`, so the negative
cumulative cash-flow outflow can never leak in. These tests pin the corrected
x1 / x1000 handling and the quarter-anchored matching, and guard against re-drift.
"""

from __future__ import annotations

from src.extract.soriana import extract_soriana_capex
from src.model.financial_model import METRICS, apply_config, load_config
from src.shared.paths import CONFIGS_DIR

_CFG = load_config(CONFIGS_DIR / "soriana.yaml")
_DEFS = apply_config(METRICS, _CFG)


def _capex(text: str):
    row = extract_soriana_capex(text, _DEFS).get("capex")
    return None if row is None else round(row.current)


def test_capex_is_phrasing_million_not_thousandfold():
    # 3Q20-style sentence; the annual estimate must NOT be captured as quarterly.
    text = "During 3Q2020, CAPEX is $668 million pesos, and the annual estimate is $1 billion pesos."
    assert _capex(text) == 668


def test_capex_invested_during_quarter_phrasing():
    text = "The capex invested during the quarter was $705 million pesos, where 55% was growth."
    assert _capex(text) == 705


def test_invested_capex_billion_still_scales():
    text = "Invested Capex of $1.815 billion pesos in the quarter."
    assert _capex(text) == 1815


def test_invested_capex_million_unchanged():
    text = "Invested Capex of $630 million pesos in the quarter."
    assert _capex(text) == 630
