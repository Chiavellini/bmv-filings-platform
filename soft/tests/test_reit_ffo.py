"""REIT FFO reconstruction from cached XBRL — src.coverage.reit_ffo. Offline; no network."""
from __future__ import annotations

from pathlib import Path

from src.coverage.reit_ffo import compute_ffo, ffo_for_slug

_YEAR = 2025

# One duration fact per concept: a clean full-fiscal-year (365-day) window ending 2025-12-31.
_ANNUAL = {"period_start": "2025-01-01", "period_end": "2025-12-31", "instant": None,
          "unit": "ISO4217:MXN", "decimals": "-3", "dimensions": None}
# A lone Q4 quarter — must never be mistaken for the annual figure.
_QUARTER = {"period_start": "2025-10-01", "period_end": "2025-12-31", "instant": None,
           "unit": "ISO4217:MXN", "decimals": "-3", "dimensions": None}
# A segment/dimensional breakdown of the same concept — must be excluded outright.
_DIMENSIONAL = {"period_start": "2025-01-01", "period_end": "2025-12-31", "instant": None,
                "unit": "ISO4217:MXN", "decimals": "-3",
                "dimensions": [{"IdDimension": "SegmentAxis", "IdItemMiembro": "RetailMember"}]}


def _fact(template: dict, value: float) -> dict:
    return {**template, "value": value}


def _facts(**concepts: dict) -> dict:
    """{concept: [fact]} from {concept_key: fact_dict} using the module's own concept names."""
    from src.coverage.reit_ffo import _CONCEPTS
    out: dict[str, list[dict]] = {}
    for key, fact in concepts.items():
        concept = _CONCEPTS[key][0]
        out[concept] = [fact]
    return out


def test_gain_year_subtracts_from_profit():
    # ProfitLoss 1,000 includes a 400 fair-value GAIN; D&A 50 → FFO = 1000 - 400 + 50 = 650.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_gain=_fact(_ANNUAL, 400.0),
        depreciation_and_amortisation=_fact(_ANNUAL, 50.0),
    )
    r = compute_ffo(facts, _YEAR)
    assert r is not None
    assert r.value == 650.0
    assert not r.corroborated
    assert "property-specific" in r.note


def test_loss_year_adds_back_to_profit():
    # ProfitLoss 1,000 includes a 400 fair-value LOSS; D&A 50 → FFO = 1000 + 400 + 50 = 1450.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_loss=_fact(_ANNUAL, 400.0),
        depreciation_and_amortisation=_fact(_ANNUAL, 50.0),
    )
    r = compute_ffo(facts, _YEAR)
    assert r is not None
    assert r.value == 1450.0


def test_corroborating_generic_tag_ships_corroborated():
    # Property tag says GAIN 400; generic ifrs-full add-back agrees (same "add to profit" sign
    # convention, so -400 for a gain) within tolerance → corroborated.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_gain=_fact(_ANNUAL, 400.0),
        fair_value_adjustment_generic=_fact(_ANNUAL, -398.0),
    )
    r = compute_ffo(facts, _YEAR)
    assert r is not None
    assert r.corroborated
    assert r.value == 1000.0 - 400.0  # D&A absent → 0.0


def test_disagreeing_tags_blank_rather_than_guess():
    # Property tag says GAIN 400; generic says only -40 (>5% apart) — can't tell which is right.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_gain=_fact(_ANNUAL, 400.0),
        fair_value_adjustment_generic=_fact(_ANNUAL, -40.0),
    )
    assert compute_ffo(facts, _YEAR) is None


def test_generic_tag_alone_ships_single_tag():
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_adjustment_generic=_fact(_ANNUAL, -400.0),
    )
    r = compute_ffo(facts, _YEAR)
    assert r is not None
    assert not r.corroborated
    assert "generic" in r.note
    assert r.value == 600.0


def test_no_fair_value_tag_blanks():
    # ProfitLoss present but no fair-value concept at all — can't tell "genuinely zero" from
    # "tagged differently" (e.g. a PP&E-holding hotel/tower FIBRA), so blank rather than assume 0.
    facts = _facts(profit_loss=_fact(_ANNUAL, 1000.0))
    assert compute_ffo(facts, _YEAR) is None


def test_missing_profit_loss_blanks():
    facts = _facts(fair_value_gain=_fact(_ANNUAL, 400.0))
    assert compute_ffo(facts, _YEAR) is None


def test_present_but_zero_treated_as_absent():
    # A mapped concept filed as an empty 0.0 placeholder line must not shadow "no tag" — mirrors
    # xbrl_facts._PREFER_NONZERO's guard for depreciation.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_gain=_fact(_ANNUAL, 0.0),
    )
    assert compute_ffo(facts, _YEAR) is None


def test_dimensional_fact_excluded():
    # Only a dimensional (segment) fact exists for the gain — must not be picked; ProfitLoss stays
    # unadjusted-only-by-nothing, so with no usable fair-value tag the result blanks.
    facts = _facts(
        profit_loss=_fact(_ANNUAL, 1000.0),
        fair_value_gain=_fact(_DIMENSIONAL, 400.0),
    )
    assert compute_ffo(facts, _YEAR) is None


def test_lone_quarter_not_mistaken_for_annual():
    # ProfitLoss has ONLY a Q4-quarter duration fact (no full-year one) — must not be picked as
    # the annual figure (this is exactly what xbrl_facts._pick_entry would do wrong here, which is
    # why compute_ffo uses its own annual-only selector).
    facts = _facts(profit_loss=_fact(_QUARTER, 300.0))
    assert compute_ffo(facts, _YEAR) is None


def test_prefers_span_closest_to_365_days():
    # Two ProfitLoss candidates for the same year: a clean 365-day one (value 1000) and a shorter
    # 335-day one (value 999, still inside the annual window) — the 365-day fact must win, verified
    # by checking which ProfitLoss value flows into the final FFO.
    from src.coverage.reit_ffo import _CONCEPTS
    pl_concept = _CONCEPTS["profit_loss"][0]
    gain_concept = _CONCEPTS["fair_value_gain"][0]
    off_span = {**_ANNUAL, "period_start": "2025-02-05", "value": 999.0}  # ~330 days
    facts = {
        pl_concept: [_fact(_ANNUAL, 1000.0), off_span],
        gain_concept: [_fact(_ANNUAL, 0.0)],  # present-but-zero → treated as absent
    }
    # No usable fair-value tag either way (the only gain fact is zero) → blanks. This test exists
    # to document/lock the selection rule; the ProfitLoss-only assertion below is the real check.
    assert compute_ffo(facts, _YEAR) is None
    only_pl = {pl_concept: facts[pl_concept]}
    # With a real (non-zero) fair-value tag added, the resulting FFO must reflect PL=1000, not 999.
    only_pl[gain_concept] = [_fact(_ANNUAL, 100.0)]
    r = compute_ffo(only_pl, _YEAR)
    assert r is not None
    assert r.value == 1000.0 - 100.0


# --- real-data golden check (offline, cached XBRL only) -------------------------------------------

_FUNO_FACTS = (Path(__file__).resolve().parents[1] / "data" / "reports" / "fibra_uno" / "xbrl"
              / "FUNO_2025-4T.json.gz")


def test_funo_golden_ffo_from_cached_xbrl():
    if not _FUNO_FACTS.exists():
        import pytest
        pytest.skip("cached FUNO XBRL filing not present in this environment")
    r = ffo_for_slug("fibra_uno")
    assert r is not None
    assert r.corroborated
    assert r.year == 2025
    # ~15,457mn (raw pesos / 1e6 per configs/fibra_uno.yaml's `unit: millions`) — property-specific
    # and generic fair-value tags agree within tolerance (1.5% apart in raw pesos).
    assert 15_000 < r.value < 16_000
