"""
test_segments_sheet.py — Segments-sheet builder (layout engine).

These tests drive ``build_segments_workbook`` directly with a tiny hand-made
DataFrame, so they are fully offline (no pipeline / no network). They check the
standardized layout: title block, period headers, hard-input values + number
formats, the auto-generated formula rows (consolidated total, YoY, %, FY sums),
graceful blanks for missing keys, and sheet-level styling.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.excel.segments_sheet import (
    SegmentRow,
    build_segments_workbook,
    segments_from_config,
    NUMFMT_MONEY,
    NUMFMT_PCT,
    _CONS_ROW,
    _FIRST_SEG_ROW,
    _BLOCK,
)


@pytest.fixture
def df():
    """Two fiscal years (2023 prior, 2024 current) for two segments."""
    rows = []
    for year, base in ((2023, 100), (2024, 110)):
        for q in (1, 2, 3, 4):
            rows.append({
                "period": f"{year}-{q}T",
                "seg_a": base + q,
                "seg_b": base * 2 + q,
                # seg_c intentionally absent → blank-cell path
            })
    return pd.DataFrame(rows)


@pytest.fixture
def segments():
    return [
        SegmentRow("Segment A", "seg_a"),
        SegmentRow("Segment B", "seg_b"),
        SegmentRow("Segment C (missing)", "seg_c"),
    ]


@pytest.fixture
def ws(df, segments):
    wb = build_segments_workbook("ACME: Test Co", segments, df)
    return wb.active


def test_single_sheet_named_segments(df, segments):
    wb = build_segments_workbook("ACME: Test Co", segments, df)
    assert wb.sheetnames == ["Segments"]


def test_title_and_subtitle(ws):
    assert ws["A2"].value == "ACME: Test Co"
    assert ws["A3"].value == "Segment data"
    assert ws["A2"].font.size == 12 and ws["A2"].font.bold


def test_period_headers_latest_two_years(ws):
    # prior = 2023, current = 2024 (auto latest two)
    assert ws["C5"].value == "1Q23A"
    assert ws["F5"].value == "4Q23A"
    assert ws["G5"].value == "FY23A"
    assert ws["H5"].value == "1Q24A"
    assert ws["K5"].value == "4Q24A"
    assert ws["L5"].value == "FY24A"
    assert ws["B5"].value == "Segments (in P$mn)"


def test_section_header(ws):
    assert ws["B6"].value == "Revenues"


def test_data_values_filled_with_money_format(ws):
    # Segment A first data row = _FIRST_SEG_ROW; values from the DataFrame.
    r = _FIRST_SEG_ROW
    assert ws[f"B{r}"].value == "Segment A"
    assert ws[f"C{r}"].value == 101          # 2023 Q1: base 100 + q 1
    assert ws[f"H{r}"].value == 111          # 2024 Q1: base 110 + q 1
    assert ws[f"C{r}"].number_format == NUMFMT_MONEY
    assert ws[f"C{r}"].font.color.rgb == "FF1F497D"


def test_fy_sum_formulas(ws):
    r = _FIRST_SEG_ROW
    assert ws[f"G{r}"].value == f"=SUM(C{r}:F{r})"
    assert ws[f"L{r}"].value == f"=SUM(H{r}:K{r})"


def test_yoy_row_is_formula(ws):
    r = _FIRST_SEG_ROW
    yoy = r + 1
    assert ws[f"B{yoy}"].value == "YoY"
    assert ws[f"H{yoy}"].value == f'=IFERROR(H{r}/C{r}-1,"N/A")'
    assert ws[f"L{yoy}"].value == f'=IFERROR(L{r}/G{r}-1,"N/A")'
    assert ws[f"H{yoy}"].number_format == NUMFMT_PCT
    assert ws[f"H{yoy}"].font.italic


def test_pct_of_consolidated_row(ws):
    r = _FIRST_SEG_ROW
    pct = r + 2
    assert ws[f"B{pct}"].value == "As % of Consolidated"
    assert ws[f"C{pct}"].value == f"=+C{r}/C${_CONS_ROW}"
    assert ws[f"C{pct}"].number_format == NUMFMT_PCT


def test_consolidated_total_sums_segment_rows(ws, segments):
    seg_rows = [_FIRST_SEG_ROW + i * _BLOCK for i in range(len(segments))]
    expected = "=" + "+".join(f"C{r}" for r in seg_rows)
    assert ws[f"C{_CONS_ROW}"].value == expected
    assert ws[f"G{_CONS_ROW}"].value == f"=SUM(C{_CONS_ROW}:F{_CONS_ROW})"
    assert ws[f"B{_CONS_ROW}"].value == "Consolidated"


def test_missing_key_renders_blank(ws, segments):
    # Segment C has no data in the DataFrame → quarterly cells are empty,
    # but its structural formulas still exist.
    r = _FIRST_SEG_ROW + 2 * _BLOCK
    assert ws[f"B{r}"].value == "Segment C (missing)"
    assert ws[f"C{r}"].value is None
    assert ws[f"H{r}"].value is None
    assert ws[f"G{r}"].value == f"=SUM(C{r}:F{r})"   # FY sum still present


def test_sheet_styling(ws):
    assert ws.sheet_view.showGridLines is False
    assert ws.column_dimensions["A"].width == 1.5
    assert round(ws.column_dimensions["B"].width, 2) == 43.16


def test_years_override(df, segments):
    wb = build_segments_workbook("ACME", segments, df, years=(2022, 2023))
    ws = wb.active
    assert ws["C5"].value == "1Q22A"
    assert ws["H5"].value == "1Q23A"


def test_segments_from_config():
    cfg = {"segments": [
        {"label": "North", "key": "net_sales_north"},
        {"label": "Mexico", "metric": "net_sales_mexico"},   # 'metric' alias
        {"label": "no key"},                                  # skipped
    ]}
    segs = segments_from_config(cfg)
    assert [(s.label, s.key) for s in segs] == [
        ("North", "net_sales_north"),
        ("Mexico", "net_sales_mexico"),
    ]


def test_empty_dataframe_does_not_crash(segments):
    wb = build_segments_workbook("ACME", segments, pd.DataFrame())
    ws = wb.active
    # No data, but structure (consolidated + segment labels) still laid out.
    assert ws["B6"].value == "Revenues"
    assert ws[f"B{_FIRST_SEG_ROW}"].value == "Segment A"
