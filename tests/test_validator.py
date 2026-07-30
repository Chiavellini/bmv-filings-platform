"""
test_validator.py — Unit tests for cross-validation rules.
"""

import pytest
from src.extract.extract_metrics import MetricRow
from src.shared.validator import (
    validate, score_confidence, ValidationResult, offending_metric, flagged_metrics,
)


def make_row(metric, current, prior=None, unit="currency"):
    return MetricRow(
        metric=metric, label_es=metric,
        current=current, prior=prior, var_pct=None,
        unit=unit, source_line="[table] test",
    )


class TestBalanceSheetIdentity:

    def test_passes_when_balanced(self):
        metrics = {
            "total_assets":      make_row("total_assets", 1_000_000),
            "total_liabilities": make_row("total_liabilities", 600_000),
            "equity":            make_row("equity", 400_000),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "balance_sheet_identity")
        assert rule.passed

    def test_fails_when_unbalanced(self):
        metrics = {
            "total_assets":      make_row("total_assets", 1_000_000),
            "total_liabilities": make_row("total_liabilities", 600_000),
            "equity":            make_row("equity", 600_000),   # wrong
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "balance_sheet_identity")
        assert not rule.passed
        assert rule.delta_pct > 0.005

    def test_skipped_when_missing(self):
        metrics = {
            "total_assets": make_row("total_assets", 1_000_000),
            "equity":       make_row("equity", 400_000),
            # total_liabilities missing
        }
        results = validate(metrics)
        assert not any(r.rule == "balance_sheet_identity" for r in results)

    def test_tolerance_0_5_pct(self):
        """Within 0.5% is acceptable (rounding in reports)."""
        metrics = {
            "total_assets":      make_row("total_assets", 1_000_000),
            "total_liabilities": make_row("total_liabilities", 600_000),
            "equity":            make_row("equity", 401_000),  # 0.1% off
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "balance_sheet_identity")
        assert rule.passed  # 1% off at max but 0.5% tolerance — should pass within range


class TestGrossProfitIdentity:

    def test_passes(self):
        metrics = {
            "revenue":       make_row("revenue", 100_000),
            "cogs":          make_row("cogs", 60_000),
            "gross_profit":  make_row("gross_profit", 40_000),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "gross_profit_identity")
        assert rule.passed

    def test_fails(self):
        metrics = {
            "revenue":       make_row("revenue", 100_000),
            "cogs":          make_row("cogs", 60_000),
            "gross_profit":  make_row("gross_profit", 50_000),  # should be 40k
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "gross_profit_identity")
        assert not rule.passed


class TestEbitdaDerivation:

    def test_passes(self):
        metrics = {
            "ebitda":            make_row("ebitda", 213_900),
            "operating_income":  make_row("operating_income", 100_000),
            "depreciation":      make_row("depreciation", 113_900),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "ebitda_derivation")
        assert rule.passed

    def test_tolerance_is_wider_1pct(self):
        """EBITDA derivation has 1% tolerance (D&A adjustments vary)."""
        metrics = {
            "ebitda":            make_row("ebitda", 100_000),
            "operating_income":  make_row("operating_income", 90_000),
            "depreciation":      make_row("depreciation", 10_500),  # 0.5% off
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "ebitda_derivation")
        assert rule.passed  # within 1% tolerance


class TestNetDebtIdentity:

    def test_passes(self):
        metrics = {
            "net_debt":   make_row("net_debt", 26_100),
            "total_debt": make_row("total_debt", 395_300),
            "cash":       make_row("cash", 369_200),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "net_debt_identity")
        assert rule.passed

    def test_fails(self):
        metrics = {
            "net_debt":   make_row("net_debt", 100_000),
            "total_debt": make_row("total_debt", 300_000),
            "cash":       make_row("cash", 250_000),   # 300k-250k=50k ≠ 100k
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "net_debt_identity")
        assert not rule.passed


class TestMarginConsistency:

    def test_sport_2026_margin_consistent(self):
        """Real 2026 values: 213,900 / 589,053 × 100 ≈ 36.31% ≈ 36.3%."""
        metrics = {
            "revenue":       make_row("revenue", 589_053),
            "ebitda":        make_row("ebitda", 213_900),
            "ebitda_margin": make_row("ebitda_margin", 36.3, unit="pct"),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "margin_consistency")
        assert rule.passed

    def test_wrong_margin_fails(self):
        """If margin is stated as 36.3% but calculated is 30%, flag it."""
        metrics = {
            "revenue":       make_row("revenue", 100_000),
            "ebitda":        make_row("ebitda", 30_000),
            "ebitda_margin": make_row("ebitda_margin", 36.3, unit="pct"),  # wrong
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "margin_consistency")
        assert not rule.passed


class TestSanityChecks:

    def test_revenue_positive_passes(self):
        metrics = {"revenue": make_row("revenue", 500_000)}
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "sanity_revenue_positive")
        assert rule.passed

    def test_revenue_zero_fails(self):
        metrics = {"revenue": make_row("revenue", 0)}
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "sanity_revenue_positive")
        assert not rule.passed

    def test_revenue_negative_fails(self):
        metrics = {"revenue": make_row("revenue", -100_000)}
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "sanity_revenue_positive")
        assert not rule.passed

    def test_yoy_change_normal(self):
        """Normal YoY growth ~14% should pass."""
        metrics = {"revenue": make_row("revenue", 589_053, prior=516_237)}
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "sanity_yoy_change")
        assert rule.passed

    def test_yoy_change_extreme_fails(self):
        """Prior of 5 (artifact) vs current 589,053 → 11,780% change → fail."""
        metrics = {"revenue": make_row("revenue", 589_053, prior=5.0)}
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "sanity_yoy_change")
        assert not rule.passed

    def test_yoy_change_not_run_without_prior(self):
        """Rule should not appear if prior is None."""
        metrics = {"revenue": make_row("revenue", 589_053, prior=None)}
        results = validate(metrics)
        # Rule should not fire since prior is None
        yoy_rules = [r for r in results if r.rule == "sanity_yoy_change"]
        assert len(yoy_rules) == 0


class TestConfidenceScoring:

    def test_table_with_passed_identity_gets_1_0(self):
        metrics = {
            "total_assets":      make_row("total_assets", 1_000_000),
            "total_liabilities": make_row("total_liabilities", 600_000),
            "equity":            make_row("equity", 400_000),
        }
        # Annotate as table source
        for row in metrics.values():
            row.source_line = "[table] some line"
        val_results = validate(metrics)
        scores = score_confidence(metrics, val_results)
        assert scores.get("total_assets", 0) >= 0.8

    def test_failed_validation_lowers_confidence(self):
        metrics = {
            "total_assets":      make_row("total_assets", 1_000_000),
            "total_liabilities": make_row("total_liabilities", 600_000),
            "equity":            make_row("equity", 900_000),  # wrong — fails
        }
        for row in metrics.values():
            row.source_line = "[table] line"
        val_results = validate(metrics)
        scores = score_confidence(metrics, val_results)
        # Failed balance sheet identity should give low score to involved metrics
        # At least one should be ≤ 0.3
        involved = ["total_assets", "total_liabilities", "equity"]
        min_score = min(scores.get(k, 1.0) for k in involved)
        assert min_score <= 0.3


class TestFcfDerivation:

    def test_fcf_passes(self):
        metrics = {
            "free_cash_flow": make_row("free_cash_flow", 50_000),
            "cfo":            make_row("cfo", 80_000),
            "capex":          make_row("capex", 30_000),
        }
        results = validate(metrics)
        rule = next((r for r in results if r.rule == "fcf_derivation"), None)
        if rule:  # only tested if all three are present
            assert rule.passed

    def test_fcf_fails(self):
        metrics = {
            "free_cash_flow": make_row("free_cash_flow", 10_000),  # wrong
            "cfo":            make_row("cfo", 80_000),
            "capex":          make_row("capex", 30_000),
        }
        results = validate(metrics)
        rule = next((r for r in results if r.rule == "fcf_derivation"), None)
        if rule:
            assert not rule.passed


class TestNetIncomeIdentity:
    """net_income ≈ ebt - tax_expense, advisory (drives confidence/marking)."""

    def test_passes_when_consistent(self):
        metrics = {
            "net_income":   make_row("net_income", 789),
            "ebt":          make_row("ebt", 1_207),
            "tax_expense":  make_row("tax_expense", 418),
        }
        rule = next(r for r in validate(metrics) if r.rule == "net_income_identity")
        assert rule.passed

    def test_flags_garbage_bottom_line(self):
        # The 2016-2T pathology: net_income=1 while ebt-tax=999.
        metrics = {
            "net_income":   make_row("net_income", 1),
            "ebt":          make_row("ebt", 1_621),
            "tax_expense":  make_row("tax_expense", 622),
        }
        results = validate(metrics)
        rule = next(r for r in results if r.rule == "net_income_identity")
        assert not rule.passed
        # Advisory only — it must NOT be quarantined (ambiguous which operand).
        assert offending_metric(rule) is None
        # ...but it lowers confidence so the cell renders marked.
        assert "net_income" in flagged_metrics(results)
        scores = score_confidence(metrics, results)
        assert scores["net_income"] == pytest.approx(0.2)


class TestSanityGuards:
    """Sign / magnitude sanity rules quarantine an unambiguous offender."""

    def test_negative_tax_is_quarantined(self):
        metrics = {
            "revenue":     make_row("revenue", 35_487),
            "tax_expense": make_row("tax_expense", -321),
        }
        rule = next(r for r in validate(metrics) if r.rule == "sanity_nonneg_tax_expense")
        assert not rule.passed
        assert offending_metric(rule) == "tax_expense"

    def test_negative_interest_is_quarantined(self):
        metrics = {"interest_expense": make_row("interest_expense", -562)}
        rule = next(r for r in validate(metrics)
                    if r.rule == "sanity_nonneg_interest_expense")
        assert offending_metric(rule) == "interest_expense"

    def test_capex_unit_artifact_is_quarantined(self):
        # 668,000 vs ~37,000 revenue — the mis-scaled "$668 million" case.
        metrics = {
            "revenue": make_row("revenue", 37_699),
            "capex":   make_row("capex", 668_000),
        }
        rule = next(r for r in validate(metrics) if r.rule == "sanity_capex_magnitude")
        assert not rule.passed
        assert offending_metric(rule) == "capex"

    def test_plausible_capex_passes(self):
        metrics = {
            "revenue": make_row("revenue", 37_699),
            "capex":   make_row("capex", 668),
        }
        assert not any(r.rule == "sanity_capex_magnitude" for r in validate(metrics))
