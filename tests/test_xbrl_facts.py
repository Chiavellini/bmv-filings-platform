"""
test_xbrl_facts.py — Tier 1 XBRL structured-facts extraction.

Uses synthetic facts dicts (the shape of a ``*_facts.json`` ["facts"] block) so
the tests are deterministic and offline. The numeric anchor is the SPORT
2026-1T ground truth: revenue 589,053 (miles) comes from a raw 589,053,000-peso
IFRS fact divided by 1000.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, MetricDef, PatternSpec, attach_concept_map
from src.extract.xbrl_facts import extract_from_xbrl, _scale, _pick_entry


def _defs(*keys):
    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


def _entry(value, *, start=None, end=None, instant=None, unit="ISO4217:MXN",
           decimals="-3", dimensions=None):
    return {
        "value": value, "period_start": start, "period_end": end,
        "instant": instant, "unit": unit, "decimals": decimals,
        "dimensions": dimensions,
    }


# ---------------------------------------------------------------------------
# Scaling
# ---------------------------------------------------------------------------

def test_monetary_value_scaled_to_miles():
    # raw pesos → miles de pesos (matches conftest revenue ground truth)
    assert _scale(_entry(589_053_000.0), "currency") == 589_053.0


def test_non_monetary_value_not_scaled():
    assert _scale(_entry(9.3, unit="pure"), "ratio") == 9.3
    assert _scale(_entry(49.0, unit="pure"), "count") == 49.0


# ---------------------------------------------------------------------------
# Basic extraction
# ---------------------------------------------------------------------------

def test_revenue_quarter_and_prior():
    facts = {
        "ifrs-full_Revenue": [
            _entry(589_053_000.0, start="2026-01-01", end="2026-03-31"),
            _entry(517_708_000.0, start="2025-01-01", end="2025-03-31"),  # prior year
        ]
    }
    rows = extract_from_xbrl(facts, _defs("revenue"), period_end="2026-03-31")
    assert "revenue" in rows
    r = rows["revenue"]
    assert r.current == 589_053.0
    assert r.prior == 517_708.0
    assert abs(r.var_pct - 13.78) < 0.05          # ~13.8% YoY
    assert r.source_line.startswith("[xbrl] ifrs-full_Revenue")


def test_quarter_preferred_over_ytd():
    # Q2 filing: the single quarter (Apr–Jun) AND the YTD (Jan–Jun) share the
    # same period_end. Tier 1 must pick the ~91-day quarter, not the 181-day YTD.
    facts = {
        "ifrs-full_Revenue": [
            _entry(700_000_000.0, start="2026-01-01", end="2026-06-30"),   # YTD
            _entry(360_000_000.0, start="2026-04-01", end="2026-06-30"),   # quarter
        ]
    }
    rows = extract_from_xbrl(facts, _defs("revenue"), period_end="2026-06-30")
    assert rows["revenue"].current == 360_000.0


def test_balance_metric_uses_instant():
    facts = {
        "ifrs-full_CashAndCashEquivalents": [
            _entry(369_200_000.0, instant="2026-03-31"),
            _entry(284_600_000.0, instant="2025-03-31"),   # prior year
        ]
    }
    rows = extract_from_xbrl(facts, _defs("cash"), period_end="2026-03-31")
    assert rows["cash"].current == 369_200.0
    assert rows["cash"].prior == 284_600.0


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------

def test_dimensional_segment_facts_excluded():
    # A segmented breakdown must not be mistaken for the consolidated total.
    facts = {
        "ifrs-full_Revenue": [
            _entry(100_000_000.0, start="2026-01-01", end="2026-03-31",
                   dimensions={"segment": "memberships"}),
            _entry(589_053_000.0, start="2026-01-01", end="2026-03-31"),  # consolidated
        ]
    }
    rows = extract_from_xbrl(facts, _defs("revenue"), period_end="2026-03-31")
    assert rows["revenue"].current == 589_053.0


def test_concept_fallback_to_second_candidate():
    # revenue maps to [ifrs-full_Revenue, ifrs-full_RevenueFromContractsWithCustomers]
    facts = {
        "ifrs-full_RevenueFromContractsWithCustomers": [
            _entry(589_053_000.0, start="2026-01-01", end="2026-03-31"),
        ]
    }
    rows = extract_from_xbrl(facts, _defs("revenue"), period_end="2026-03-31")
    assert rows["revenue"].current == 589_053.0


def test_missing_concept_yields_no_row():
    rows = extract_from_xbrl({}, _defs("revenue"), period_end="2026-03-31")
    assert rows == {}


def test_metric_without_xbrl_concepts_skipped():
    # ebitda has no clean IFRS tag → never answered by Tier 1
    ebitda = next(m for m in attach_concept_map(METRICS) if m.key == "ebitda")
    assert ebitda.xbrl_concepts == []
    facts = {"ifrs-full_Revenue": [_entry(589_053_000.0, start="2026-01-01", end="2026-03-31")]}
    rows = extract_from_xbrl(facts, [ebitda], period_end="2026-03-31")
    assert "ebitda" not in rows


def test_no_period_end_picks_latest():
    facts = {
        "ifrs-full_Revenue": [
            _entry(517_708_000.0, start="2025-01-01", end="2025-03-31"),
            _entry(589_053_000.0, start="2026-01-01", end="2026-03-31"),  # latest
        ]
    }
    rows = extract_from_xbrl(facts, _defs("revenue"))
    assert rows["revenue"].current == 589_053.0
