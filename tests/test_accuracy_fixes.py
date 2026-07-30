"""test_accuracy_fixes.py — Regression tests for the extraction-accuracy pass
against data/ground_truth (low-performer campaign: Bimbo, Sport, Liverpool, Walmex).

Each test pins a specific cell that was previously wrong/missing so the fix can't
silently regress. See docs/NIGHT_LOG.md for the per-company writeup.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, apply_config, load_config
from src.extract.extract_metrics import extract_metrics_segmented


def _extract(company: str, filename: str) -> dict:
    cfg = load_config(ROOT / "configs" / f"{company}.yaml")
    defs = apply_config(METRICS, cfg)
    path = ROOT / "data" / "reports" / company / filename
    if not path.exists():
        pytest.skip(f"Report not found: {path}")
    return extract_metrics_segmented(path.read_text(encoding="utf-8"), defs, cfg)


# ---------------------------------------------------------------------------
# BIMBO — consolidated-row anchoring (bypass Q4 FINANCIAL-SUMMARY full-year grab)
# ---------------------------------------------------------------------------

def test_bimbo_4q19_revenue_is_quarter_not_full_year():
    """4Q19 opens with a FY FINANCIAL SUMMARY ('Net Sales 291,926'); revenue must be
    the quarter (75,457) read from the consolidated segment row, not the annual."""
    rows = _extract("bimbo", "2019-4T.md")
    assert rows["revenue"].current == pytest.approx(75_457.0, rel=1e-4)


def test_bimbo_2q17_operating_income_not_prior_year_column():
    """2016-2017 numbers-first layout: the section-anchored consolidated row must give
    the current quarter (4,589), not the prior-year column (4,853)."""
    rows = _extract("bimbo", "2017-2T.md")
    assert rows["operating_income"].current == pytest.approx(4_589.0, rel=1e-4)


def test_bimbo_2q18_revenue_na_is_net_sales_not_ebitda_row():
    """The parser renders 'NorthAmerica' with no space; the \\s* label + net-sales
    ordering must give NA net sales (36,903), not the NA EBITDA row (1,081)."""
    rows = _extract("bimbo", "2018-2T.md")
    assert rows["revenue_na"].current == pytest.approx(36_903.0, rel=1e-4)


# ---------------------------------------------------------------------------
# SPORT — segment-revenue label fixes (values in miles_mxn)
# ---------------------------------------------------------------------------

def test_sport_1q18_sponsorships_table_row_with_comerciales():
    """P&L row is 'Ingresos por patrocinios y otras actividades comerciales 10,797 ...';
    the optional 'comerciales' word must not block the table match."""
    rows = _extract("sport", "2018-1T.md")
    v = rows.get("revenue_sponsorships")
    assert v is not None and v.current == pytest.approx(10_797.0, rel=1e-4)


# ---------------------------------------------------------------------------
# LIVERPOOL — full-peso scale in a %-bearing legacy release table
# ---------------------------------------------------------------------------

def test_liverpool_full_peso_release_scale():
    """2016-3T prints full pesos inside a %-table ('21,767,113,000'); values >= 1e9
    must be divided by 1e6 -> millions (21,767.1), not 1e3."""
    from src.extract.liverpool import _normalize_currency
    assert _normalize_currency(21_767_113_000.0, release_scale=True) == pytest.approx(21_767.113, rel=1e-6)
    # A genuine thousands-scale value stays on the /1000 path (unchanged behavior).
    assert _normalize_currency(21_767_000.0, release_scale=True) == pytest.approx(21_767.0, rel=1e-6)


# ---------------------------------------------------------------------------
# WALMEX — proof-cited definitional exclusions (divested Suburbia/Medimart)
# ---------------------------------------------------------------------------

def test_walmex_store_definitional_exclusions():
    """total_stores/_mexico for 1Q14A-3Q18A are EXCLUDED (source includes divested
    Suburbia/Medimart); post-divestiture periods are NOT excluded."""
    from src.eval.compare_extractions import excluded_reason
    cfg = load_config(ROOT / "configs" / "walmex.yaml")
    assert excluded_reason(cfg, "total_stores", "1Q14A")  # divested-formats era
    assert excluded_reason(cfg, "total_stores_mexico", "3Q18A")
    assert excluded_reason(cfg, "revenue", "1Q16A")  # Suburbia discontinued-ops restatement
    # 4Q18A onward carries no divested format -> must NOT be excluded.
    assert excluded_reason(cfg, "total_stores", "4Q18A") is None
    # ebitda exclusion is scoped to the two confirmed FAILs only.
    assert excluded_reason(cfg, "ebitda", "1Q16A")
    assert excluded_reason(cfg, "ebitda", "3Q15A") is None


def test_walmex_sss_grew_phrasing():
    """SSS regex must accept the 'grew X% in Mexico' phrasing (2023 releases), not just
    'growth of X%'."""
    rows = _extract("walmex", "Walmex_1Q23_Results_Release.md")
    assert rows["sss_mexico"].current == pytest.approx(8.7, abs=0.01)


# ---------------------------------------------------------------------------
# LACOMER — round 2
# ---------------------------------------------------------------------------

def test_lacomer_2q18_total_stores_headline_not_post_quarter_opening():
    """'opera 62 tiendas' (quarter-end) must win over 'cuenta con 63' (post-quarter opening)."""
    rows = _extract("lacomer", "La-comer-2do-Trimestre-2018.md")
    assert rows["total_stores"].current == 62


def test_lacomer_3q16_sss_negative_sign():
    """'Decremento ... de (0.6%)' must be captured as -0.6, not +0.6."""
    rows = _extract("lacomer", "3t16_bmv.md")
    assert rows["sss_lacomer"].current == pytest.approx(-0.6, abs=0.01)


def test_lacomer_city_market_cafe_era_gated():
    """City Market Café (introduced 3Q25) is excluded before 3Q25."""
    from src.eval.compare_extractions import excluded_reason
    cfg = load_config(ROOT / "configs" / "lacomer.yaml")
    assert excluded_reason(cfg, "stores_city_market_cafe", "1Q20A")  # era gate
    assert excluded_reason(cfg, "stores_city_market_cafe", "1Q25A")  # pre-Q3-2025
    assert excluded_reason(cfg, "stores_city_market_cafe", "3Q25A") is None  # first café quarter


# ---------------------------------------------------------------------------
# CHEDRAUI — round 2
# ---------------------------------------------------------------------------

def test_chedraui_1q25_revenue_us_not_ebitda_row():
    """revenue_us 1Q25 must read the full-peso US segment note (40,847), not the US
    EBITDA row (3,073) that the over-broad EE.UU. pattern used to grab."""
    rows = _extract("chedraui", "2025-1T.md")
    assert rows["revenue_us"].current == pytest.approx(40_847.4, rel=1e-3)


def test_chedraui_3q23_ebitda_uafida_label():
    """3Q23 labels EBITDA as 'UAFIDA'/'CONSOLIDADO'; must capture the current column 5,712."""
    rows = _extract("chedraui", "2023-3T.md")
    assert rows["ebitda"].current == pytest.approx(5_712.0, rel=1e-3)


# ---------------------------------------------------------------------------
# KOF — round 2 (fix the pre-existing regression)
# ---------------------------------------------------------------------------

def test_kof_3q18_revenue_clean_reported_line():
    """3Q18 must read the reported summary line 44,148, not the split-number '1' the
    search tier grabbed from '1 37,041'."""
    rows = _extract("kof", "2018-3T.md")
    assert rows["revenue"].current == pytest.approx(44_148.0, rel=1e-3)


# ---------------------------------------------------------------------------
# ROUND 3 — config-only
# ---------------------------------------------------------------------------

def test_kimber_pre2023_segments_excluded():
    """Absolute segment revenue (IFRS-8 note) is disclosed only from 2023; earlier
    releases give growth % only, so pre-2023 segment cells are EXCLUDED."""
    from src.eval.compare_extractions import excluded_reason
    cfg = load_config(ROOT / "configs" / "kimber.yaml")
    assert excluded_reason(cfg, "revenue_consumer", "2Q19A")
    assert excluded_reason(cfg, "revenue_exports", "1Q22A")
    assert excluded_reason(cfg, "revenue_consumer", "1Q23A") is None  # first absolute-note year


def test_sport_revenue_sports_two_row_components():
    """revenue_sports = 'Ingresos deportivos' + 'Otros ingresos del negocio'; the two P&L
    component rows must parse to their quarter columns (4Q19: 28,392 + 39,737)."""
    rows = _extract("sport", "2019-4T.md")
    assert rows["revenue_deportivos"].current == pytest.approx(28_392.0)
    assert rows["revenue_otros_negocio"].current == pytest.approx(39_737.0)


def test_sport_net_churn_targeted_exclusion():
    """net_churn is excluded only for the quarters that genuinely omit it (verified no
    'deserción neta' mention); reporting-present quarters are NOT excluded."""
    from src.eval.compare_extractions import excluded_reason
    cfg = load_config(ROOT / "configs" / "sport.yaml")
    assert excluded_reason(cfg, "net_churn", "3Q24A")   # genuinely absent
    assert excluded_reason(cfg, "net_churn", "4Q21A") is None  # reported


def test_lab_2q19_otc_no_scale_catastrophe():
    """The space-permissive 'Total' regex_table fallback (which merged a whole row into
    1.9e23) is removed; 2Q19 revenue_otc must not emit an astronomical value."""
    rows = _extract("lab", "2019-2T.md")
    v = rows.get("revenue_otc")
    assert v is None or v.current is None or v.current < 100_000  # sane magnitude, no 1e23


def test_bimbo_2q16_duplicate_file_excluded():
    """2016-2T.md is a duplicate of the 1Q16 report; all 2Q16 Bimbo cells are excluded
    (parse_gap) — but 1Q16 itself is not."""
    from src.eval.compare_extractions import excluded_reason
    cfg = load_config(ROOT / "configs" / "bimbo.yaml")
    assert excluded_reason(cfg, "revenue", "2Q16A")
    assert excluded_reason(cfg, "ebitda", "2Q16A")
    assert excluded_reason(cfg, "revenue", "1Q16A") is None
