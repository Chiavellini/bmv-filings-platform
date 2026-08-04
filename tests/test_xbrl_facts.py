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
from src.extract.tiered_extract import PeriodSource, extract_metrics_tiered
from src.extract.xbrl_facts import (
    extract_comparative_observations_from_xbrl,
    extract_from_xbrl,
    fact_value_divisor_for,
    pesos_per_unit_for_facts,
    _scale,
    _pick_entry,
)


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


def test_native_usd_facts_already_in_millions_are_not_divided_again():
    facts = {
        "ifrs-full_Assets": [_entry(32_668.23, instant="2022-12-31", unit="ISO4217:USD")],
        "ifrs-full_Revenue": [
            _entry(13_870.3, start="2022-01-01", end="2022-12-31", unit="ISO4217:USD"),
        ],
    }
    cfg = {"company": {"currency": "USD", "unit": "millions"}}

    divisor = fact_value_divisor_for(cfg, facts, currency_mode="native")

    assert divisor == 1.0
    assert _scale(facts["ifrs-full_Revenue"][0], "currency", divisor) == 13_870.3


def test_native_full_unit_quarterly_facts_keep_configured_scaling():
    facts = {
        "ifrs-full_Revenue": [
            _entry(
                589_053_000.0,
                start="2026-01-01",
                end="2026-03-31",
            ),
        ],
    }
    cfg = {"company": {"currency": "MXN", "unit": "miles"}}

    divisor = fact_value_divisor_for(cfg, facts, currency_mode="native")

    assert divisor == 1e3
    assert _scale(facts["ifrs-full_Revenue"][0], "currency", divisor) == 589_053.0


def test_convert_to_mxn_applies_fx_once_for_already_million_usd_facts():
    facts = {
        "ifrs-full_Assets": [_entry(32_668.23, instant="2022-12-31", unit="ISO4217:USD")],
        "ifrs-full_Revenue": [
            _entry(13_870.3, start="2022-01-01", end="2022-12-31", unit="ISO4217:USD"),
        ],
    }
    cfg = {"company": {"currency": "USD", "unit": "millions"}}

    divisor = pesos_per_unit_for_facts(cfg, facts)

    assert _scale(facts["ifrs-full_Revenue"][0], "currency", divisor) == 259_374.61


def test_convert_to_mxn_keeps_full_unit_fx_scaling():
    facts = {
        "ifrs-full_Revenue": [
            _entry(
                13_870_300_000.0,
                start="2022-10-01",
                end="2022-12-31",
                unit="ISO4217:USD",
            ),
        ],
    }
    cfg = {"company": {"currency": "USD", "unit": "millions"}}

    divisor = pesos_per_unit_for_facts(cfg, facts)

    assert _scale(facts["ifrs-full_Revenue"][0], "currency", divisor) == 259_374.61


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


# ---------------------------------------------------------------------------
# Typed later-filing comparatives / restatements
# ---------------------------------------------------------------------------

def test_later_xbrl_filing_emits_unambiguous_prior_quarter_observation():
    facts = {
        "ifrs-full_Revenue": [
            _entry(260_000_000.0, start="2024-10-01", end="2024-12-31"),
            _entry(205_000_000.0, start="2024-07-01", end="2024-09-30"),
        ]
    }

    observations = extract_comparative_observations_from_xbrl(
        facts,
        _defs("revenue"),
        report_period="2024-4T",
        period_end="2024-12-31",
        pesos_per_unit=1e6,
        expected_currency="MXN",
        source_document_id="q4-doc",
    )

    assert len(observations) == 1
    observation = observations[0]
    assert observation.observed_period == "2024-3T"
    assert observation.report_period == "2024-4T"
    assert observation.value == 205.0
    assert observation.source_tier == "xbrl"
    assert observation.trusted is True
    assert observation.source_document_id == "q4-doc"


def test_comparative_observation_skips_ytd_and_conflicting_duplicate_contexts():
    facts = {
        "ifrs-full_Revenue": [
            _entry(260_000_000.0, start="2024-10-01", end="2024-12-31"),
            # Same Q3 context disagrees inside one filing: unsafe, so no Q3 assertion.
            _entry(205_000_000.0, start="2024-07-01", end="2024-09-30"),
            _entry(206_000_000.0, start="2024-07-01", end="2024-09-30"),
            # Nine-month YTD must never be projected onto the Q3 workbook cell.
            _entry(600_000_000.0, start="2024-01-01", end="2024-09-30"),
        ]
    }

    observations = extract_comparative_observations_from_xbrl(
        facts,
        _defs("revenue"),
        report_period="2024-4T",
        period_end="2024-12-31",
        pesos_per_unit=1e6,
        expected_currency="MXN",
    )

    assert observations == []


def test_later_annual_xbrl_filing_emits_typed_fy_comparative():
    facts = {
        "ifrs-full_Revenue": [
            _entry(1_100_000_000.0, start="2024-01-01", end="2024-12-31"),
            _entry(1_005_000_000.0, start="2023-01-01", end="2023-12-31"),
        ]
    }

    observations = extract_comparative_observations_from_xbrl(
        facts,
        _defs("revenue"),
        report_period="2024-FY",
        period_end="2024-12-31",
        pesos_per_unit=1e6,
        expected_currency="MXN",
    )

    assert [(item.observed_period, item.period_kind.value, item.value)
            for item in observations] == [("2023-FY", "fy", 1005.0)]


def test_gmexico_production_fy_comparative_keeps_native_usd_millions():
    # Values and contexts from GMEXICO_2023-FY_facts.json. The generated facts
    # artifact is estate data (not a test fixture), so pin its production-shaped
    # records here to keep this regression test clean-clone deterministic.
    facts = {
        "ifrs-full_Revenue": [
            _entry(14_776.8, start="2021-01-01", end="2021-12-31", unit="ISO4217:USD"),
            _entry(13_870.3, start="2022-01-01", end="2022-12-31", unit="ISO4217:USD"),
            _entry(14_366.9, start="2023-01-01", end="2023-12-31", unit="ISO4217:USD"),
        ],
    }
    cfg = {"company": {"currency": "USD", "unit": "millions"}}

    result = extract_metrics_tiered(
        PeriodSource(
            period="2023-FY",
            period_end="2023-12-31",
            facts=facts,
            facts_document_id="GMEXICO_2023-FY_facts.json",
        ),
        _defs("revenue"),
        cfg,
        tiers={"xbrl"},
    )

    revenue_observations = [
        (item.observed_period, item.value)
        for item in result.observations
        if item.metric == "revenue"
    ]
    assert revenue_observations == [("2021-FY", 14_776.8), ("2022-FY", 13_870.3)]
