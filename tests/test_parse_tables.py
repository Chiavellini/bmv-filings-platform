"""
test_parse_tables.py — Tier 2 table-cell extraction.

The matching logic (match_metrics_from_rows) is pure and PDF-free, so most tests
feed synthetic (label, cells) rows. One smoke test runs the full pdfplumber path
against a real report PDF when one is on disk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.model.financial_model import METRICS, attach_concept_map
from src.extract.parse_tables import (
    _looks_numeric, _norm, _label_score, match_metrics_from_rows, extract_from_tables,
    TableBlock, match_metrics_from_blocks,
)


def _defs(*keys):
    defs = attach_concept_map(METRICS)
    return [m for m in defs if m.key in keys]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_looks_numeric():
    assert _looks_numeric("589,053")
    assert _looks_numeric("(66,000)")
    assert _looks_numeric("14.3%")
    assert _looks_numeric("49")
    assert not _looks_numeric("1")          # lone footnote digit rejected
    assert not _looks_numeric("Ingresos")
    assert not _looks_numeric("-")


def test_norm_strips_accents_and_case():
    assert _norm("Utilidad de Operación") == "utilidad de operacion"
    assert _norm("Total de ingresos:") == "total de ingresos"


def test_label_score_prefix_match():
    assert _label_score("total de ingresos 1", "total de ingresos") == 1.0
    assert _label_score("costo de ventas", "costo de ventas") == 1.0
    assert _label_score("otros gastos", "total de ingresos") < 0.85


# ---------------------------------------------------------------------------
# Row → metric matching
# ---------------------------------------------------------------------------

def test_revenue_row_current_and_prior():
    rows = [("Total de ingresos", ["589,053", "517,708", "71,345", "13.8%"])]
    out = match_metrics_from_rows(rows, _defs("revenue"))
    assert out["revenue"].current == 589_053.0
    assert out["revenue"].prior == 517_708.0
    assert out["revenue"].source_line.startswith("[table]")


def test_ifrs16_columns_take_first_value():
    # 7-column IFRS-16 row: con-IFRS is the first numeric → operating_income 107,900
    rows = [("Utilidad de operación", ["107,900", "95,000", "13.6", "55,300", "40,000"])]
    out = match_metrics_from_rows(rows, _defs("operating_income"))
    assert out["operating_income"].current == 107_900.0


# ---------------------------------------------------------------------------
# Header-aware block matching (period selection) + positional fallback
# ---------------------------------------------------------------------------

def _block(header_by_col, label, cell_pairs):
    return TableBlock(header_by_col=header_by_col, rows=[(label, cell_pairs)])


def test_blocks_select_target_quarter_column():
    # Current quarter (1T16) is the SECOND numeric; positional would mispick the
    # prior-year column. Period selection must take the 1T16 column.
    block = _block(
        {0: "1T15", 1: "1T16"},
        "Total de ingresos",
        [(0, "517,708"), (1, "589,053")],
    )
    out = match_metrics_from_blocks([block], _defs("revenue"), period="1Q16A")
    assert out["revenue"].current == 589_053.0
    assert out["revenue"].prior == 517_708.0


def test_blocks_without_period_fall_back_to_positional():
    block = _block(
        {0: "1T15", 1: "1T16"},
        "Total de ingresos",
        [(0, "517,708"), (1, "589,053")],
    )
    out = match_metrics_from_blocks([block], _defs("revenue"))   # no period
    assert out["revenue"].current == 517_708.0                   # first numeric


def test_blocks_unparseable_header_falls_back_to_positional():
    block = _block(
        {},                                                      # no usable header
        "Total de ingresos",
        [(0, "517,708"), (1, "589,053")],
    )
    out = match_metrics_from_blocks([block], _defs("revenue"), period="1Q16A")
    assert out["revenue"].current == 517_708.0                   # positional


def test_negative_parenthetical_value():
    rows = [("Utilidad neta", ["(66,000)", "41,209"])]
    out = match_metrics_from_rows(rows, _defs("net_income"))
    assert out["net_income"].current == -66_000.0


def test_footnote_digit_between_label_and_value_ignored():
    # word-clustering would yield ["1", "131,568", ...]; the lone "1" is dropped
    rows = [("Efectivo y equivalentes de efectivo", ["131,568", "120,000"])]
    out = match_metrics_from_rows(rows, _defs("cash"))
    assert out["cash"].current == 131_568.0


def test_no_match_returns_empty():
    rows = [("Renglón irrelevante", ["123,456"])]
    out = match_metrics_from_rows(rows, _defs("revenue"))
    assert out == {}


def test_table_scale_scales_currency():
    # lacomer income table prints full pesos; scale 1e-6 → millions
    rows = [("Total de ingresos", ["6,183,912,000", "4,921,621,000"])]
    out = match_metrics_from_rows(rows, _defs("revenue"), table_scale=1e-6)
    assert abs(out["revenue"].current - 6183.912) < 0.01


def test_table_scale_skips_noncurrency():
    defs = _defs("shares_outstanding")   # unit=count, must NOT be scaled
    rows = [(defs[0].label_es, ["1,234,000"])]
    out = match_metrics_from_rows(rows, defs, table_scale=1e-6)
    assert out["shares_outstanding"].current == 1_234_000


def test_skip_keys_excludes_metric():
    rows = [("Total de ingresos", ["589,053", "517,708"])]
    out = match_metrics_from_rows(rows, _defs("revenue"),
                                  skip_keys=frozenset({"revenue"}))
    assert out == {}


def test_falls_back_to_label_es_when_no_aliases():
    # construct a def with no aliases; matching uses label_es
    defs = _defs("inventory")
    defs[0].aliases.clear()
    rows = [("Inventarios", ["12,345"])]
    out = match_metrics_from_rows(rows, defs)
    assert out["inventory"].current == 12_345.0


def test_semantic_dictionary_alias_matches_row_label():
    rows = [("Resultado operativo", ["107,900", "95,000"])]
    out = match_metrics_from_rows(rows, _defs("operating_income"))
    assert out["operating_income"].current == 107_900.0


def test_semantic_dictionary_rejects_derived_rows():
    rows = [
        ("YoY", ["13.8%"]),
        ("As % of Total", ["10.0%"]),
        ("Margin", ["25.0%"]),
    ]
    out = match_metrics_from_rows(rows, _defs("revenue", "gross_profit"))
    assert out == {}


def test_currency_metric_rejects_percentage_only_semantic_row():
    rows = [
        ("Ingresos y EBITDA de", ["25.3%", "38.2%"]),
        ("Gastos de Operacion con respecto a los ingresos sin IFRS", ["16", "66.3%"]),
        ("% de los Gastos de Operacion con respecto a los ingresos sin IFRS", ["16"]),
    ]
    out = match_metrics_from_rows(rows, _defs("revenue", "ebitda"))
    assert out == {}


# ---------------------------------------------------------------------------
# Full pdfplumber path (real PDF smoke test)
# ---------------------------------------------------------------------------

def test_extract_from_tables_on_real_pdf():
    pytest.importorskip("pdfplumber")
    pdf = ROOT / "data" / "reports" / "sport" / "2024-1T.pdf"
    if not pdf.exists():
        pytest.skip("sample report PDF not on disk")
    out = extract_from_tables(pdf, _defs("revenue", "cash", "total_assets"))
    # revenue should be located and within 1% of the known 517,708 (miles)
    assert "revenue" in out, "Tier 2 should locate revenue in a real report table"
    assert abs(out["revenue"].current - 517_708) / 517_708 < 0.01


def test_extract_from_tables_missing_file():
    assert extract_from_tables(ROOT / "does_not_exist.pdf", _defs("revenue")) == {}
