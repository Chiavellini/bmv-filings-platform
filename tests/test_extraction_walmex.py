"""
test_extraction_walmex.py — Integration tests for WALMEX metric extraction.

Ground-truth values manually verified against Walmex_2Q25_Release.md.
All financial values in millions MXN (as reported in WALMEX tables).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, apply_config, load_config
from src.extract.extract_metrics import extract_metrics, extract_metrics_segmented, _split_sections


# ---------------------------------------------------------------------------
# Ground truth — 2Q25 (manually verified)
# ---------------------------------------------------------------------------

WALMEX_2Q25: dict[str, float] = {
    # Consolidated P&L (table, exact)
    "revenue":            246_254,
    "gross_profit":        59_406,
    "operating_income":    17_267,
    "ebitda":              23_495,
    "net_sales":          244_556,
    "income_before_other": 16_982,
    # Mexico segment (table, exact)
    "revenue_mexico":     202_883,
    "gross_profit_mexico": 48_790,
    "operating_income_mexico": 14_444,
    "ebitda_mexico":       19_500,
    # CAM segment (table, exact)
    "revenue_cam":         43_371,
    "gross_profit_cam":    10_616,
    "operating_income_cam":  2_823,
    "ebitda_cam":           3_995,
    # Store counts (table, exact)
    "total_stores":         4_124,
    "total_stores_mexico":  3_191,
    "total_stores_cam":       933,
    # Sales floor (table, exact)
    "sales_floor_mexico": 7_032_065,
    "sales_floor_cam":      847_628,
    # KPI prose (±0.5% tolerance)
    "sss_mexico":             4.4,
    "sss_cam":                4.0,
    "ticket_mexico":          6.0,
    "traffic_mexico":        -1.4,
    "ecommerce_gmv_growth":  19.0,  # 1H value (Q2 column lost to PDF interleaving)
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def walmex_cfg():
    return load_config(ROOT / "configs" / "walmex.yaml")


@pytest.fixture(scope="module")
def walmex_metric_defs(walmex_cfg):
    return apply_config(METRICS, walmex_cfg)


@pytest.fixture(scope="module")
def walmex_2q25_text():
    path = ROOT / "data" / "reports" / "walmex" / "Walmex_2Q25_Release.md"
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def walmex_2q25_rows(walmex_2q25_text, walmex_metric_defs, walmex_cfg):
    return extract_metrics_segmented(walmex_2q25_text, walmex_metric_defs, walmex_cfg)


@pytest.fixture(scope="module")
def walmex_1q23_text():
    """Pre-segment-header report (no Mexico/CAM section headers)."""
    path = ROOT / "data" / "reports" / "walmex" / "Walmex_1Q23_Results_Release.md"
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _assert_exact(rows, key, expected):
    assert key in rows, f"Key '{key}' missing from extracted metrics"
    actual = rows[key].current
    assert actual == expected, f"{key}: expected {expected}, got {actual}"


def _assert_close(rows, key, expected, tol=0.005):
    assert key in rows, f"Key '{key}' missing from extracted metrics"
    actual = rows[key].current
    assert actual is not None, f"{key}: got None, expected {expected}"
    if expected == 0:
        assert actual == 0
        return
    rel_err = abs(actual - expected) / abs(expected)
    assert rel_err <= tol, (
        f"{key}: expected {expected}, got {actual} (rel error {rel_err:.3%})"
    )


# ---------------------------------------------------------------------------
# Section-splitting
# ---------------------------------------------------------------------------

class TestSectionSplit:

    def test_mexico_section_present(self, walmex_2q25_text, walmex_cfg):
        anchors = [
            (s["name"], s["header_pattern"])
            for s in walmex_cfg.get("sections", [])
        ]
        sections = _split_sections(walmex_2q25_text, anchors)
        assert "mexico" in sections
        assert len(sections["mexico"]) > 200

    def test_cam_section_present(self, walmex_2q25_text, walmex_cfg):
        anchors = [
            (s["name"], s["header_pattern"])
            for s in walmex_cfg.get("sections", [])
        ]
        sections = _split_sections(walmex_2q25_text, anchors)
        assert "cam" in sections
        assert len(sections["cam"]) > 200

    def test_consolidated_comes_first(self, walmex_2q25_text, walmex_cfg):
        anchors = [
            (s["name"], s["header_pattern"])
            for s in walmex_cfg.get("sections", [])
        ]
        sections = _split_sections(walmex_2q25_text, anchors)
        # "Total Revenues 246,254" appears only in the consolidated section
        assert "246,254" in sections.get("consolidated", "")

    def test_no_segment_keys_for_pre_header_report(
        self, walmex_1q23_text, walmex_metric_defs, walmex_cfg
    ):
        rows = extract_metrics_segmented(walmex_1q23_text, walmex_metric_defs, walmex_cfg)
        # Pre-4Q23 reports lack section headers; segment keys must not appear
        assert "revenue_mexico" not in rows or rows["revenue_mexico"].current is None
        assert "revenue_cam" not in rows or rows["revenue_cam"].current is None


# ---------------------------------------------------------------------------
# Consolidated P&L
# ---------------------------------------------------------------------------

class TestConsolidated:

    def test_revenue(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "revenue", 246_254)

    def test_gross_profit(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "gross_profit", 59_406)

    def test_operating_income(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "operating_income", 17_267)

    def test_ebitda(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "ebitda", 23_495)

    def test_net_sales(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "net_sales", 244_556)

    def test_income_before_other(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "income_before_other", 16_982)

    def test_prior_revenue(self, walmex_2q25_rows):
        assert walmex_2q25_rows["revenue"].prior == 227_415

    def test_var_pct_revenue(self, walmex_2q25_rows):
        assert abs(walmex_2q25_rows["revenue"].var_pct - 8.3) < 0.1


# ---------------------------------------------------------------------------
# Mexico segment
# ---------------------------------------------------------------------------

class TestMexicoSegment:

    def test_revenue_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "revenue_mexico", 202_883)

    def test_gross_profit_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "gross_profit_mexico", 48_790)

    def test_operating_income_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "operating_income_mexico", 14_444)

    def test_ebitda_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "ebitda_mexico", 19_500)

    def test_prior_revenue_mexico(self, walmex_2q25_rows):
        assert walmex_2q25_rows["revenue_mexico"].prior == 191_345

    def test_var_pct_revenue_mexico(self, walmex_2q25_rows):
        assert abs(walmex_2q25_rows["revenue_mexico"].var_pct - 6.0) < 0.1


# ---------------------------------------------------------------------------
# Central America segment
# ---------------------------------------------------------------------------

class TestCAMSegment:

    def test_revenue_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "revenue_cam", 43_371)

    def test_gross_profit_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "gross_profit_cam", 10_616)

    def test_operating_income_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "operating_income_cam", 2_823)

    def test_ebitda_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "ebitda_cam", 3_995)

    def test_prior_revenue_cam(self, walmex_2q25_rows):
        assert walmex_2q25_rows["revenue_cam"].prior == 36_070

    def test_var_pct_revenue_cam(self, walmex_2q25_rows):
        assert abs(walmex_2q25_rows["revenue_cam"].var_pct - 20.2) < 0.1


# ---------------------------------------------------------------------------
# Store counts
# ---------------------------------------------------------------------------

class TestStores:

    def test_total_stores(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "total_stores", 4_124)

    def test_total_stores_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "total_stores_mexico", 3_191)

    def test_total_stores_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "total_stores_cam", 933)

    def test_stores_sum(self, walmex_2q25_rows):
        """total_stores_mexico + total_stores_cam == total_stores."""
        mx = walmex_2q25_rows["total_stores_mexico"].current
        cam = walmex_2q25_rows["total_stores_cam"].current
        total = walmex_2q25_rows["total_stores"].current
        assert mx + cam == total, f"{mx} + {cam} != {total}"


# ---------------------------------------------------------------------------
# Sales floor
# ---------------------------------------------------------------------------

class TestSalesFloor:

    def test_sales_floor_mexico(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "sales_floor_mexico", 7_032_065)

    def test_sales_floor_cam(self, walmex_2q25_rows):
        _assert_exact(walmex_2q25_rows, "sales_floor_cam", 847_628)


# ---------------------------------------------------------------------------
# Prose KPIs
# ---------------------------------------------------------------------------

class TestProseKPIs:

    def test_sss_mexico(self, walmex_2q25_rows):
        _assert_close(walmex_2q25_rows, "sss_mexico", 4.4)

    def test_sss_cam(self, walmex_2q25_rows):
        _assert_close(walmex_2q25_rows, "sss_cam", 4.0)

    def test_ticket_mexico(self, walmex_2q25_rows):
        _assert_close(walmex_2q25_rows, "ticket_mexico", 6.0)

    def test_traffic_mexico(self, walmex_2q25_rows):
        _assert_close(walmex_2q25_rows, "traffic_mexico", -1.4)

    def test_ecommerce_gmv_growth(self, walmex_2q25_rows):
        # Captures 1H value (19%) due to PDF two-column interleaving losing Q2 column
        _assert_close(walmex_2q25_rows, "ecommerce_gmv_growth", 19.0)


# ---------------------------------------------------------------------------
# Metric definition completeness
# ---------------------------------------------------------------------------

class TestMetricDefs:

    def test_walmex_overrides_revenue_pattern(self, walmex_metric_defs):
        """WALMEX-specific pattern must be first for revenue."""
        rev = next(m for m in walmex_metric_defs if m.key == "revenue")
        assert rev.patterns, "revenue has no patterns"
        first_regex = rev.patterns[0].regex
        assert "Total\\s+Revenues?" in first_regex, (
            f"First pattern should be WALMEX table pattern, got: {first_regex[:80]}"
        )

    def test_custom_metrics_registered(self, walmex_metric_defs):
        keys = {m.key for m in walmex_metric_defs}
        for expected in [
            "total_stores", "total_stores_mexico", "total_stores_cam",
            "sales_floor_mexico", "sales_floor_cam",
            "sss_mexico", "sss_cam", "ticket_mexico", "traffic_mexico",
            "ecommerce_gmv_growth", "net_sales", "income_before_other",
        ]:
            assert expected in keys, f"Custom metric '{expected}' not in metric defs"


# ---------------------------------------------------------------------------
# 1Q26 — consolidated P&L moved into "Appendix 1" behind the Mexico/CAM section
# tables. Anchored patterns (Net Sales / Cost of Sales) must still pick the
# consolidated table, not the CAM section "Total Revenues" (40,660).
# ---------------------------------------------------------------------------

WALMEX_1Q26_CONSOLIDATED: dict[str, float] = {
    "revenue":          245_018,
    "gross_profit":      59_593,
    "operating_income":  18_472,
    "ebitda":            24_979,
}


@pytest.fixture(scope="module")
def walmex_1q26_text():
    path = ROOT / "data" / "reports" / "walmex" / "Walmex_1Q26_Release.md"
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def walmex_1q26_rows(walmex_1q26_text, walmex_metric_defs, walmex_cfg):
    return extract_metrics_segmented(walmex_1q26_text, walmex_metric_defs, walmex_cfg)


class TestConsolidated1Q26:

    @pytest.mark.parametrize("key,expected", list(WALMEX_1Q26_CONSOLIDATED.items()))
    def test_consolidated_value(self, walmex_1q26_rows, key, expected):
        row = walmex_1q26_rows.get(key)
        assert row is not None and row.current is not None, f"{key} not extracted"
        assert row.current == expected, (
            f"{key}: expected consolidated {expected}, got {row.current} "
            f"(likely grabbed a Mexico/CAM section value)"
        )
