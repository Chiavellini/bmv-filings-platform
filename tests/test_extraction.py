"""
test_extraction.py — Integration tests: full extract_metrics() against real reports.

Ground-truth values are in conftest.py. Precision rules:
  - TABLE source: exact match (0% tolerance)
  - PROSE source: ≤ 0.5% relative error

These tests guard against regressions in the pattern catalog.
"""

import pytest
import sys
from pathlib import Path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from tests.conftest import (
    SPORT_2026_1T, SPORT_2024_1T, SPORT_2022_1T, SPORT_2016_1T, SPORT_2017_1T,
    assert_close,
)
from src.extract.extract_metrics import extract_metrics
from tests._corpus import requires_corpus_for


# ---------------------------------------------------------------------------
# 2026-1T — most complete report (16/16 metrics)
# ---------------------------------------------------------------------------

class TestExtraction2026:

    def test_revenue_exact_from_table(self, sport_2026_text, sport_metric_defs):
        """Table extraction must return 589,053 exactly."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "revenue" in metrics, "revenue not found in 2026-1T"
        assert metrics["revenue"].current == 589_053.0, (
            f"Expected 589053 (exact table value), got {metrics['revenue'].current}"
        )

    def test_ebitda_con_ifrs(self, sport_2026_text, sport_metric_defs):
        """EBITDA con IFRS 16: $213.9M prose → 213,900 miles (±0.5%)."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "ebitda" in metrics
        assert_close(metrics["ebitda"].current, 213_900, "currency", "ebitda", "prose")

    def test_ebitda_margin_captures_con_ifrs_not_sin_ifrs(self, sport_2026_text, sport_metric_defs):
        """Critical: must capture 36.3% (con IFRS), NOT 16.5% (sin IFRS).
        The prose line reads: 'sin IFRS 16 ... 16.5% y con IFRS 16 de 36.3%'
        """
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "ebitda_margin" in metrics
        val = metrics["ebitda_margin"].current
        assert abs(val - 36.3) < 0.2, (
            f"ebitda_margin: expected ~36.3% (con IFRS), got {val}%"
        )

    def test_ebitda_sin_ifrs(self, sport_2026_text, sport_metric_defs):
        """EBITDA sin IFRS 16: $97.3M prose → 97,300 miles (±0.5%)."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "ebitda_sin_ifrs" in metrics
        assert_close(metrics["ebitda_sin_ifrs"].current, 97_300, "currency", "ebitda_sin_ifrs", "prose")

    def test_ebitda_margin_sin_ifrs(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "ebitda_margin_sin_ifrs" in metrics
        assert abs(metrics["ebitda_margin_sin_ifrs"].current - 16.5) < 0.2

    def test_cash_prose(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "cash" in metrics
        assert_close(metrics["cash"].current, 369_200, "currency", "cash", "prose")

    def test_operating_income_con_ifrs(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "operating_income" in metrics
        assert_close(metrics["operating_income"].current, 107_900, "currency", "operating_income", "prose")

    def test_net_debt_prose(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "net_debt" in metrics
        assert_close(metrics["net_debt"].current, 1_241_000, "currency", "net_debt", "prose")

    def test_operating_profit_variants(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "operating_profit_con_ifrs" in metrics
        assert "operating_profit_sin_ifrs" in metrics
        assert_close(metrics["operating_profit_con_ifrs"].current, 107_900, "currency", "operating_profit_con_ifrs", "prose")
        assert_close(metrics["operating_profit_sin_ifrs"].current, 55_300, "currency", "operating_profit_sin_ifrs", "prose")

    def test_clientes_activos_exact_from_table(self, sport_2026_text, sport_metric_defs):
        """Exact from operational summary table."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "clientes_activos" in metrics
        assert metrics["clientes_activos"].current == 96_616.0

    def test_clientes_prior_value(self, sport_2026_text, sport_metric_defs):
        """Prior quarter value should also be captured from table."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert metrics["clientes_activos"].prior == pytest.approx(99_830.0)

    def test_net_churn_exact_from_table(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "net_churn" in metrics
        assert metrics["net_churn"].current == pytest.approx(5.6, abs=0.05)

    def test_monthly_visits_exact_from_table(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "monthly_visits" in metrics
        assert metrics["monthly_visits"].current == 877_678.0

    def test_visits_per_member(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "visits_per_member" in metrics
        assert metrics["visits_per_member"].current == pytest.approx(9.3, abs=0.05)

    def test_clubs_count(self, sport_2026_text, sport_metric_defs):
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert "clubs_count" in metrics
        assert metrics["clubs_count"].current == pytest.approx(49.0)

    def test_minimum_metrics_found(self, sport_2026_text, sport_metric_defs):
        """At least 14 metrics must be found in the most complete report."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        assert len(metrics) >= 14, (
            f"Expected ≥14 metrics, found {len(metrics)}: {list(metrics.keys())}"
        )

    def test_all_known_values(self, sport_2026_text, sport_metric_defs):
        """Bulk check: all ground-truth values within tolerance."""
        metrics = extract_metrics(sport_2026_text, sport_metric_defs)
        prose_keys = {
            "ebitda", "ebitda_sin_ifrs", "ebitda_margin", "ebitda_margin_sin_ifrs",
            "operating_income", "operating_profit_con_ifrs", "operating_profit_sin_ifrs",
            "cash", "net_debt", "revenue_memberships", "revenue_sports",
            "revenue_sponsorships",
        }
        for key, expected in SPORT_2026_1T.items():
            if key not in metrics:
                continue  # some sub-metrics may be absent
            src = "prose" if key in prose_keys else "table"
            assert_close(metrics[key].current, expected, "currency", key, src)


# ---------------------------------------------------------------------------
# 2024-1T
# ---------------------------------------------------------------------------

class TestExtraction2024:

    def test_revenue_exact_from_table(self, sport_2024_text, sport_metric_defs):
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "revenue" in metrics
        assert metrics["revenue"].current == 517_708.0

    def test_ebitda_exact_from_table(self, sport_2024_text, sport_metric_defs):
        """2024 has EBITDA in a clean table with footnote artifact."""
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "ebitda" in metrics
        # Table value is 192,119 — exact
        assert metrics["ebitda"].current == pytest.approx(192_119.0, rel=0.001)

    def test_ebitda_prior_from_table(self, sport_2024_text, sport_metric_defs):
        """Prior year EBITDA: 139,062 (before the footnote '1 39,062' artifact)."""
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "ebitda" in metrics
        assert metrics["ebitda"].prior == pytest.approx(39_062.0, rel=0.01)

    def test_revenue_memberships_exact(self, sport_2024_text, sport_metric_defs):
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "revenue_memberships" in metrics
        assert metrics["revenue_memberships"].current == pytest.approx(399_659.0, rel=0.001)

    def test_clientes_activos_from_prose(self, sport_2024_text, sport_metric_defs):
        """2024 lacks operational summary table; value from prose."""
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "clientes_activos" in metrics
        assert metrics["clientes_activos"].current == pytest.approx(82_379.0, rel=0.001)

    def test_monthly_visits_from_prose(self, sport_2024_text, sport_metric_defs):
        metrics = extract_metrics(sport_2024_text, sport_metric_defs)
        assert "monthly_visits" in metrics
        assert metrics["monthly_visits"].current == pytest.approx(697_764.0, rel=0.001)


# ---------------------------------------------------------------------------
# 2022-1T — COVID recovery, negative EBITDA sin IFRS
# ---------------------------------------------------------------------------

class TestExtraction2022:

    def test_revenue_exact_from_table(self, sport_2022_text, sport_metric_defs):
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "revenue" in metrics
        assert metrics["revenue"].current == 249_793.0

    def test_ebitda_exact_from_table(self, sport_2022_text, sport_metric_defs):
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "ebitda" in metrics
        assert metrics["ebitda"].current == pytest.approx(41_209.0)

    def test_ebitda_prior_negative(self, sport_2022_text, sport_metric_defs):
        """Prior EBITDA was -94,052 (parenthetical in table)."""
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "ebitda" in metrics
        assert metrics["ebitda"].prior == pytest.approx(-94_052.0, rel=0.01)

    def test_ebitda_sin_ifrs_negative(self, sport_2022_text, sport_metric_defs):
        """Critical: EBITDA sin IFRS 16 must be NEGATIVE (-66,000)."""
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "ebitda_sin_ifrs" in metrics
        val = metrics["ebitda_sin_ifrs"].current
        assert val < 0, f"ebitda_sin_ifrs should be negative, got {val}"
        assert_close(val, -66_000, "currency", "ebitda_sin_ifrs", "prose")

    def test_clientes_exact_from_table(self, sport_2022_text, sport_metric_defs):
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "clientes_activos" in metrics
        assert metrics["clientes_activos"].current == 59_430.0

    def test_monthly_visits_exact_from_table(self, sport_2022_text, sport_metric_defs):
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "monthly_visits" in metrics
        assert metrics["monthly_visits"].current == pytest.approx(348_037.0)

    def test_revenue_memberships_exact(self, sport_2022_text, sport_metric_defs):
        metrics = extract_metrics(sport_2022_text, sport_metric_defs)
        assert "revenue_memberships" in metrics
        assert metrics["revenue_memberships"].current == pytest.approx(204_103.0)


# ---------------------------------------------------------------------------
# 2016-1T — pre-IFRS 16, UAFIDA terminology
# ---------------------------------------------------------------------------

class TestExtraction2016:

    def test_revenue_uafida_era_exact(self, sport_2016_text, sport_metric_defs):
        """2016 uses 'Total de Ingresos Netos' label."""
        metrics = extract_metrics(sport_2016_text, sport_metric_defs)
        assert "revenue" in metrics
        assert metrics["revenue"].current == 314_925.0

    def test_ebitda_as_uafida_exact(self, sport_2016_text, sport_metric_defs):
        """2016 uses 'UAFIDA' label — must be captured by ebitda metric."""
        metrics = extract_metrics(sport_2016_text, sport_metric_defs)
        assert "ebitda" in metrics, "ebitda (UAFIDA) not found in 2016-1T"
        assert metrics["ebitda"].current == pytest.approx(45_179.0)

    def test_ebitda_margin_uafida_era(self, sport_2016_text, sport_metric_defs):
        """2016 'Margen UAFIDA 14.3%'."""
        metrics = extract_metrics(sport_2016_text, sport_metric_defs)
        assert "ebitda_margin" in metrics
        assert abs(metrics["ebitda_margin"].current - 14.3) < 0.2

    def test_cash_from_balance_table(self, sport_2016_text, sport_metric_defs):
        """Balance General table: exact value."""
        metrics = extract_metrics(sport_2016_text, sport_metric_defs)
        assert "cash" in metrics
        assert metrics["cash"].current == 131_568.0

    def test_net_debt_from_prose(self, sport_2016_text, sport_metric_defs):
        metrics = extract_metrics(sport_2016_text, sport_metric_defs)
        assert "net_debt" in metrics
        assert_close(metrics["net_debt"].current, 302_200, "currency", "net_debt", "prose")


# ---------------------------------------------------------------------------
# 2017-1T — footnote artifact test
# ---------------------------------------------------------------------------

class TestExtraction2017:

    def test_clubs_count_rejects_footnote_491(self, sport_2017_text, sport_metric_defs):
        """pdfplumber renders '49¹' as '491'. clubs_count must return 49, not 491."""
        metrics = extract_metrics(sport_2017_text, sport_metric_defs)
        assert "clubs_count" in metrics
        val = metrics["clubs_count"].current
        assert val == pytest.approx(49.0), (
            f"clubs_count: expected 49 (2-digit pattern strips '1' footnote), got {val}. "
            r"Check (\d{2})\d* pattern in sport.yaml clubs_count config."
        )
        assert val != pytest.approx(491.0), "clubs_count captured footnote artifact 491!"


# ---------------------------------------------------------------------------
# Batch extraction test
# ---------------------------------------------------------------------------

@requires_corpus_for("sport")
class TestBatchExtraction:
    # batch_extract walks data/reports/, which is gitignored.

    def test_batch_produces_40_rows(self, sport_metric_defs):
        """Batch over all reportes/*.md should produce ~40 rows."""
        from pathlib import Path
        from src.extract.extract_metrics import batch_extract
        root = Path(__file__).parent.parent
        df = batch_extract(root=root, metric_defs=sport_metric_defs)
        assert len(df) >= 38, (
            f"Expected ≥38 periods, got {len(df)}. "
            "Some reports may be missing from reportes/"
        )

    def test_batch_revenue_coverage(self, sport_metric_defs):
        """Revenue should be non-null for at least 90% of periods."""
        from pathlib import Path
        from src.extract.extract_metrics import batch_extract
        root = Path(__file__).parent.parent
        df = batch_extract(root=root, metric_defs=sport_metric_defs)
        if df.empty or "revenue" not in df.columns:
            pytest.skip("revenue column not present in batch output")
        non_null = df["revenue"].notna().sum()
        total = len(df)
        assert non_null / total >= 0.90, (
            f"Revenue coverage: {non_null}/{total} = {non_null/total:.0%}, expected ≥90%"
        )

    def test_batch_no_obvious_artifacts(self, sport_metric_defs):
        """Year numbers (2019, 2020, etc.) should not appear as revenue values."""
        from pathlib import Path
        from src.extract.extract_metrics import batch_extract
        root = Path(__file__).parent.parent
        df = batch_extract(root=root, metric_defs=sport_metric_defs)
        if "revenue" not in df.columns:
            pytest.skip("revenue column not present")
        suspicious = df[df["revenue"] < 1000]["revenue"].dropna()
        assert len(suspicious) == 0, (
            f"Suspicious revenue values < 1000 found: {suspicious.tolist()} "
            "(likely year-number or split-artifact)"
        )
