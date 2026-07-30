"""
test_extraction_lacomer.py — Integration tests for LACOMER metric extraction.

Ground-truth values manually verified against lacomer/BMV_4T25.md (Q4 2025).
Financial values in millions MXN (BMV filings are in full pesos; patterns use
multiplier: 0.000001 to convert). Store counts and sales floor in raw units.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, apply_config, load_config
from src.extract.extract_metrics import extract_metrics_segmented


# ---------------------------------------------------------------------------
# Ground truth — Q4 2025 (from lacomer/BMV_4T25.md)
# ---------------------------------------------------------------------------

# Financial values: full_pesos × 0.000001 = millions MXN
# Table QTR-current column: 12,503,178,000 → 12,503.178 million
LACOMER_4T25: dict[str, float] = {
    "revenue":         12_503.178,
    "gross_profit":     3_808.890,
    "operating_income":   712.565,
    "net_income":         506.215,
    "ebitda":           1_110.0,     # prose: "$1,110 millones"
    "eps":                  0.47,    # quarterly EPS
    "sss_lacomer":          6.4,     # prose: "crecimiento de 6.4%"
    # Store counts (exact)
    "total_stores":        92,
    "stores_la_comer":     39,
    "stores_fresko":       22,
    "stores_city_market":  17,
    "stores_sumesa":       13,
    "stores_city_market_cafe": 1,
    # Sales floor m² (exact)
    "sales_floor_total":    418_462,
    "sales_floor_la_comer": 266_344,
    "sales_floor_fresko":    82_477,
    "sales_floor_sumesa":    10_303,
    "sales_floor_city_market": 58_941,
}

LACOMER_4T25_PRIOR: dict[str, float] = {
    "revenue":    11_422.001,
    "gross_profit": 3_421.207,
    "operating_income": 568.731,
    "net_income":   407.906,
    "eps":             0.38,
}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def lacomer_cfg():
    return load_config(ROOT / "configs" / "lacomer.yaml")


@pytest.fixture(scope="module")
def lacomer_metric_defs(lacomer_cfg):
    return apply_config(METRICS, lacomer_cfg)


@pytest.fixture(scope="module")
def lacomer_4t25_text():
    path = ROOT / "data" / "reports" / "lacomer" / "BMV_4T25.md"
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lacomer_4t25_rows(lacomer_4t25_text, lacomer_metric_defs, lacomer_cfg):
    return extract_metrics_segmented(lacomer_4t25_text, lacomer_metric_defs, lacomer_cfg)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOL = 1e-3  # 0.1% — tighter than prose tolerance since table values are exact


def _assert_close(rows, key, expected, tol=_TOL):
    assert key in rows, f"Key '{key}' missing from extracted metrics"
    actual = rows[key].current
    assert actual is not None, f"{key}: got None, expected {expected}"
    if expected == 0:
        assert actual == 0
        return
    rel_err = abs(actual - expected) / abs(expected)
    assert rel_err <= tol, f"{key}: expected {expected}, got {actual} (rel err {rel_err:.4%})"


def _assert_exact(rows, key, expected):
    assert key in rows, f"Key '{key}' missing from extracted metrics"
    actual = rows[key].current
    assert actual == expected, f"{key}: expected {expected}, got {actual}"


# ---------------------------------------------------------------------------
# Financial P&L — table-extracted (0.1% tolerance from float rounding)
# ---------------------------------------------------------------------------

class TestPnL:

    def test_revenue_current(self, lacomer_4t25_rows):
        _assert_close(lacomer_4t25_rows, "revenue", 12_503.178)

    def test_revenue_prior(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows["revenue"]
        assert r.prior is not None
        assert abs(r.prior - 11_422.001) / 11_422.001 < _TOL

    def test_gross_profit(self, lacomer_4t25_rows):
        _assert_close(lacomer_4t25_rows, "gross_profit", 3_808.890)

    def test_gross_profit_prior(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows["gross_profit"]
        assert abs(r.prior - 3_421.207) / 3_421.207 < _TOL

    def test_operating_income(self, lacomer_4t25_rows):
        _assert_close(lacomer_4t25_rows, "operating_income", 712.565)

    def test_net_income(self, lacomer_4t25_rows):
        _assert_close(lacomer_4t25_rows, "net_income", 506.215)

    def test_eps(self, lacomer_4t25_rows):
        _assert_close(lacomer_4t25_rows, "eps", 0.47)

    def test_eps_prior(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows["eps"]
        assert abs(r.prior - 0.38) < 0.005


class TestEbitda:

    def test_ebitda_prose(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows.get("ebitda")
        assert r is not None, "ebitda missing"
        assert r.current is not None
        # "$1,110 millones" → 1110; allow 0.5% for prose rounding
        assert abs(r.current - 1110) / 1110 <= 0.005, f"ebitda: expected ~1110, got {r.current}"

    def test_ebitda_source_is_prose(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows["ebitda"]
        assert r.source_line and "prose" in r.source_line.lower()


# ---------------------------------------------------------------------------
# SSS
# ---------------------------------------------------------------------------

class TestSSS:

    def test_sss_lacomer(self, lacomer_4t25_rows):
        r = lacomer_4t25_rows.get("sss_lacomer")
        assert r is not None, "sss_lacomer missing"
        assert abs(r.current - 6.4) < 0.05, f"sss: expected 6.4, got {r.current}"


# ---------------------------------------------------------------------------
# Store counts
# ---------------------------------------------------------------------------

class TestStores:

    def test_total_stores(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "total_stores", 92.0)

    def test_stores_la_comer(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "stores_la_comer", 39.0)

    def test_stores_fresko(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "stores_fresko", 22.0)

    def test_stores_city_market(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "stores_city_market", 17.0)

    def test_stores_sumesa(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "stores_sumesa", 13.0)

    def test_stores_city_market_cafe(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "stores_city_market_cafe", 1.0)

    def test_stores_sum_equals_total(self, lacomer_4t25_rows):
        total = lacomer_4t25_rows["total_stores"].current
        parts = sum(
            lacomer_4t25_rows[k].current
            for k in ["stores_la_comer", "stores_fresko", "stores_city_market",
                      "stores_sumesa", "stores_city_market_cafe"]
        )
        assert parts == total, f"sum of formats ({parts}) != total_stores ({total})"


# ---------------------------------------------------------------------------
# Sales floor
# ---------------------------------------------------------------------------

class TestSalesFloor:

    def test_sales_floor_total(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "sales_floor_total", 418_462.0)

    def test_sales_floor_la_comer(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "sales_floor_la_comer", 266_344.0)

    def test_sales_floor_fresko(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "sales_floor_fresko", 82_477.0)

    def test_sales_floor_sumesa(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "sales_floor_sumesa", 10_303.0)

    def test_sales_floor_city_market(self, lacomer_4t25_rows):
        _assert_exact(lacomer_4t25_rows, "sales_floor_city_market", 58_941.0)

    def test_sales_floor_formats_sum(self, lacomer_4t25_rows):
        total = lacomer_4t25_rows["sales_floor_total"].current
        parts = sum(
            lacomer_4t25_rows[k].current
            for k in ["sales_floor_la_comer", "sales_floor_fresko",
                      "sales_floor_sumesa", "sales_floor_city_market"]
        )
        # City Market Café (397 m²) not individually tracked but total should be close
        assert abs(parts - total) <= 500, f"format sum {parts} differs from total {total} by more than 500 m²"


# ---------------------------------------------------------------------------
# Metric definition checks
# ---------------------------------------------------------------------------

class TestMetricDefs:

    def test_revenue_pattern_skips_ytd(self, lacomer_metric_defs):
        rev = next(m for m in lacomer_metric_defs if m.key == "revenue")
        first_regex = rev.patterns[0].regex
        # Should have non-capturing groups to skip YTD columns
        assert "\\d,]+" in first_regex or "[\\d,]+" in first_regex, \
            "Revenue pattern should skip YTD columns with non-capturing groups"

    def test_revenue_multiplier(self, lacomer_metric_defs):
        rev = next(m for m in lacomer_metric_defs if m.key == "revenue")
        assert rev.patterns[0].multiplier == 0.000001, \
            f"Expected multiplier 0.000001, got {rev.patterns[0].multiplier}"

    def test_custom_metrics_registered(self, lacomer_metric_defs):
        keys = {m.key for m in lacomer_metric_defs}
        for expected in [
            "sss_lacomer", "total_stores",
            "stores_la_comer", "stores_fresko", "stores_city_market",
            "stores_sumesa", "stores_city_market_cafe",
            "sales_floor_total", "sales_floor_la_comer", "sales_floor_fresko",
        ]:
            assert expected in keys, f"Custom metric '{expected}' not registered"
