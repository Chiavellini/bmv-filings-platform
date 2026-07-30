"""Unit coverage for two generic robustness levers:

* `consolidation_rank` — prefers a consolidated/total row over a same-metric
  segment row (Phase 3), so a segmented filer's consolidated figure wins without
  per-company `sections` config.
* `offending_metric` — names the single value to quarantine for a failed
  data-driven validator rule (Phase 2), so the cascade drops the implausible
  value (→ honest MISS) without dropping the correct operands it was checked on.
"""

from __future__ import annotations

from src.extract.semantic_search import consolidation_rank
from src.shared.validator import offending_metric, ValidationResult


# --- Phase 3: consolidation ranking ----------------------------------------

def test_consolidated_total_beats_segment():
    # bimbo: the consolidated row must outrank the Mexico segment row.
    consolidated = consolidation_rank("Total Net sales (Including Eliminations and others)")
    segment = consolidation_rank("Net Sales Mexico")
    assert consolidated > segment


def test_grupo_and_consolidado_rank_high():
    assert consolidation_rank("Grupo Comercial Chedraui") >= 2
    assert consolidation_rank("Consolidado") >= 2


def test_region_rows_demoted():
    for region in ("Mexico", "North America", "Suburbia", "EAA", "LatAm"):
        assert consolidation_rank(region) < 0


def test_plain_metric_label_is_neutral():
    assert consolidation_rank("Revenue") == 0
    assert consolidation_rank("EBITDA") == 0


def test_empty_label_is_neutral():
    assert consolidation_rank("") == 0


# --- Phase 2: validator-gate offender selection ----------------------------

def _vr(rule, involved, passed=False):
    return ValidationResult(rule=rule, passed=passed, expected=None, actual=None,
                            delta_pct=None, message="", metrics_involved=involved)


def test_segment_exceeds_total_pops_segment_not_revenue():
    r = _vr("segment_not_exceeds_total", ["revenue_mexico", "revenue"])
    assert offending_metric(r) == "revenue_mexico"


def test_revenue_positive_pops_revenue():
    r = _vr("sanity_revenue_positive", ["revenue"])
    assert offending_metric(r) == "revenue"


def test_ambiguous_identity_rules_pop_nothing():
    # Identity / segment-sum / YoY rules implicate several operands without saying
    # which is wrong → must NOT auto-quarantine (would risk dropping good values).
    for rule in ("gross_profit_identity", "ebitda_derivation", "revenue_segment_sum",
                 "sanity_yoy_change", "balance_sheet_identity"):
        assert offending_metric(_vr(rule, ["revenue", "cogs", "gross_profit"])) is None


def test_passed_rule_pops_nothing():
    assert offending_metric(_vr("segment_not_exceeds_total", ["revenue_mexico", "revenue"], passed=True)) is None
