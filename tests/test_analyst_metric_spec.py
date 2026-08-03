from __future__ import annotations

import csv

import pytest

from src.excel.analyst_spec import (
    AnalystMetricSheetError,
    compare_metric_kinds,
    compare_metric_keys,
    compare_metric_sequences,
    load_analyst_metric_sheet,
)


def test_reads_legacy_multi_company_csv_block(tmp_path):
    path = tmp_path / "Metrics.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerows(
            [
                ["Revenues", "", "FEMSA"],
                ["Net Sales", "", ""],
                ["YoY", "", ""],
                ["Segments (in P$mn)", "", "KOF"],
                ["Consolidated", "", ""],
            ]
        )

    spec = load_analyst_metric_sheet(path, "Fomento Economico Mexicano", aliases=("FEMSA",))
    assert spec.labels == ("Revenues", "Net Sales", "YoY")
    assert spec.keys == (None, None, None)
    assert spec.kinds == (None, None, None)
    assert len(spec.sha256) == 64


def test_reads_normalized_section_label_csv(tmp_path):
    path = tmp_path / "request.csv"
    path.write_text(
        "section,label,key\nRevenue,Net Sales,revenue\nRevenue,YoY,\nProfit,EBITDA,ebitda\n",
        encoding="utf-8",
    )
    spec = load_analyst_metric_sheet(path, "Any Company")
    assert spec.labels == ("Revenue", "Net Sales", "YoY", "Profit", "EBITDA")
    assert spec.keys == (None, "revenue", None, None, "ebitda")
    assert spec.kinds == ("section", "row", "row", "section", "row")


def test_reads_requested_xlsx_worksheet_and_preserves_declared_keys(tmp_path):
    from openpyxl import Workbook

    path = tmp_path / "request.xlsx"
    workbook = Workbook()
    workbook.active.title = "Ignore"
    requested = workbook.create_sheet("Requested Metrics")
    requested.append(["section", "label", "key"])
    requested.append(["Revenue", "Net Sales", "revenue"])
    requested.append(["Revenue", "YoY", ""])
    workbook.save(path)

    spec = load_analyst_metric_sheet(
        path,
        "Any Company",
        sheet_name="Requested Metrics",
    )
    assert spec.sheet_name == "Requested Metrics"
    assert spec.labels == ("Revenue", "Net Sales", "YoY")
    assert spec.keys == (None, "revenue", None)
    assert spec.kinds == ("section", "row", "row")


def test_legacy_block_requires_company_marker(tmp_path):
    path = tmp_path / "request.csv"
    path.write_text("Revenue,,OTHER\nNet Sales,,\n", encoding="utf-8")
    with pytest.raises(AnalystMetricSheetError, match="not found"):
        load_analyst_metric_sheet(path, "MISSING")


def test_malformed_xlsx_is_reported_as_analyst_sheet_error(tmp_path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"not a zip workbook")
    with pytest.raises(AnalystMetricSheetError, match="cannot read analyst metric sheet"):
        load_analyst_metric_sheet(path, "Any Company")


def test_sequence_comparison_is_order_sensitive_but_whitespace_tolerant():
    ok = compare_metric_sequences(
        ["Revenue", "Net  Sales", "YoY"],
        [" revenue ", "Net Sales", "YOY"],
    )
    assert ok.matches

    bad = compare_metric_sequences(
        ["Revenue", "Net Sales", "YoY"],
        ["Revenue", "YoY", "Net Sales"],
    )
    assert not bad.matches
    assert bad.first_difference == "row 2: analyst='Net Sales', outline='YoY'"


def test_declared_analyst_keys_are_authoritative_but_blank_keys_are_optional():
    ok = compare_metric_keys(
        ["Revenue", "Net Sales", "YoY"],
        [None, "revenue", None],
        [None, "REVENUE", None],
    )
    assert ok.matches

    bad = compare_metric_keys(
        ["Revenue", "Net Sales", "YoY"],
        [None, "revenue", None],
        [None, "ebitda", None],
    )
    assert not bad.matches
    assert "analyst key='revenue', outline key='ebitda'" in bad.first_difference


def test_normalized_sheet_row_cannot_be_reclassified_as_outline_section():
    fidelity = compare_metric_kinds(
        ["Revenue", "EBITDA"],
        ["row", "row"],
        ["section", "row"],
    )
    assert not fidelity.matches
    assert "analyst kind='row', outline kind='section'" in fidelity.first_difference
