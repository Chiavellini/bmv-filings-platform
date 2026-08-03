"""
test_segments_outline.py — outline-driven Segments generator (offline).

Covers parse_outline classification (incl. the repeated-label disambiguation
that the Walmex model needs) and build_outline_workbook layout/formulas/blanks.
No pipeline / no network — the builder is driven by a tiny hand-made DataFrame.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from src.excel.segments_sheet import (
    RowSpec, parse_outline, build_outline_workbook,
    MetricLayoutMap, NUMFMT_MONEY, NUMFMT_PCT, _BLUE,
)

OUTLINE = """\
Revenues

Total
YoY
Mexico
YoY
As % of Consolidated

Gross Profit

Consolidated
YoY
Margin
Mexico
YoY
As % of Consolidated
"""

SECTIONS = ["Revenues", "Gross Profit"]
MAPPING = {
    "Total": "revenue",
    "Mexico": "revenue_mexico",                 # under Revenues
    "Gross Profit/Consolidated": "gross_profit",
    "Gross Profit/Mexico": "gross_profit_mexico",
}


def _kinds(rows):
    return [(r.kind, r.label, r.key, r.derived) for r in rows]


def test_parse_classifies_sections_data_derived_spacer():
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    by_label = {}
    for r in rows:
        by_label.setdefault(r.label, []).append(r)
    # Sections
    assert by_label["Revenues"][0].kind == "section"
    assert by_label["Gross Profit"][0].kind == "section"
    # Derived subtypes
    yoy = [r for r in rows if r.label == "YoY"]
    assert yoy and all(r.kind == "derived" and r.derived == "yoy" for r in yoy)
    margin = [r for r in rows if r.label == "Margin"][0]
    assert margin.kind == "derived" and margin.derived == "margin"
    pct = [r for r in rows if r.label == "As % of Consolidated"][0]
    assert pct.kind == "derived" and pct.derived == "pct_consolidated"
    assert any(r.kind == "spacer" for r in rows)


def test_parse_disambiguates_repeated_mexico_by_section():
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    mex = [r for r in rows if r.label == "Mexico"]
    assert [r.key for r in mex] == ["revenue_mexico", "gross_profit_mexico"]
    # "Consolidated" only exists under Gross Profit → section-qualified key
    cons = [r for r in rows if r.label == "Consolidated"]
    assert [r.key for r in cons] == ["gross_profit"]


def test_parse_unmapped_nonsection_is_blank_data():
    rows = parse_outline("Stores\nBodega Aurrera\nWalmart\n",
                         sections=["Stores"], mapping={})
    kinds = {r.label: r.kind for r in rows}
    assert kinds["Stores"] == "section"
    assert kinds["Bodega Aurrera"] == "data"   # unmapped leaf → blank data, not a header
    assert kinds["Walmart"] == "data"


@pytest.fixture
def df():
    rows = []
    for year, base in ((2023, 100), (2024, 110)):
        for q in (1, 2, 3, 4):
            rows.append({"period": f"{year}-{q}T",
                         "revenue": base + q, "revenue_mexico": base // 2 + q,
                         "gross_profit": base // 2 + q, "gross_profit_mexico": base // 4 + q})
    return pd.DataFrame(rows)


@pytest.fixture
def ws(df):
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    unit_map = {"revenue": "currency", "revenue_mexico": "currency",
                "gross_profit": "currency", "gross_profit_mexico": "currency"}
    wb = build_outline_workbook("ACME: Co", rows, df, unit_map=unit_map)
    return wb.active


def _find(ws, label):
    for r in range(6, ws.max_row + 1):
        if ws[f"B{r}"].value == label:
            return r
    raise AssertionError(f"{label!r} not found")


def test_low_confidence_cells_get_flag_comment_no_fill(df):
    """df.attrs['confidence'] drives marking: flagged/low → comment ONLY (no
    amber fill, no amber font) — the comment indicator is the red flag."""
    df = df.copy()
    df.attrs["confidence"] = {
        ("2023-1T", "revenue"): {"confidence": 0.2, "flagged": True,
                                 "source": "[search] alias"},
        ("2024-1T", "revenue"): {"confidence": 1.0, "flagged": False,
                                 "source": "[table] cell"},
    }
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    ws = build_outline_workbook("ACME: Co", rows, df,
                                unit_map={"revenue": "currency"}).active
    r = _find(ws, "Total")
    # 2023 block starts at C (2023-1T = C); 2024 block at H (2024-1T = H).
    low, high = ws[f"C{r}"], ws[f"H{r}"]
    assert low.comment is not None and "Unverified" in low.comment.text
    assert low.font.color.rgb == _BLUE            # normal confident blue, not amber
    assert low.fill.fgColor.rgb != "FFFFF2CC"     # no amber highlight
    assert high.font.color.rgb == _BLUE
    assert high.comment is None


def test_series_suspect_cells_get_flag_comment(df):
    """df.attrs['suspects'] (sign/range/magnitude, from series_checks) also
    renders as a red-flag comment naming the check that fired."""
    df = df.copy()
    df.attrs["suspects"] = {
        ("2023-2T", "revenue"): "magnitude outlier vs surrounding periods (4 vs neighbor median 800)",
    }
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    ws = build_outline_workbook("ACME: Co", rows, df,
                                unit_map={"revenue": "currency"}).active
    r = _find(ws, "Total")
    cell = ws[f"D{r}"]                            # 2023-2T
    assert cell.comment is not None and "magnitude outlier" in cell.comment.text
    assert cell.font.color.rgb == _BLUE


def test_unresolved_cells_note_failed_verification(df):
    """A verify_status='unresolved' pin ships flagged, with the comment saying
    verification was attempted."""
    df = df.copy()
    df.attrs["confidence"] = {
        ("2023-1T", "revenue"): {"confidence": 0.8, "flagged": False,
                                 "source": "[table] cell",
                                 "verify_status": "unresolved",
                                 "verify_note": "table garbled in source PDF"},
    }
    df.attrs["suspects"] = {("2023-1T", "revenue"): "failed a cross-check"}
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    ws = build_outline_workbook("ACME: Co", rows, df,
                                unit_map={"revenue": "currency"}).active
    r = _find(ws, "Total")
    cell = ws[f"C{r}"]
    assert cell.comment is not None
    assert "could not resolve" in cell.comment.text
    assert "table garbled" in cell.comment.text


def test_legend_has_no_amber_fill(ws):
    assert "comment marker" in str(ws["B4"].value)
    assert ws["B4"].fill.fgColor.rgb != "FFFFF2CC"


# ---------------------------------------------------------------------------
# Auto-generated check rows (segment-sum + accounting identities)
# ---------------------------------------------------------------------------

@pytest.fixture
def df_reconciling():
    """revenue = mexico + usa exactly; gross_profit = revenue − cogs exactly."""
    rows = []
    for year, base in ((2023, 100), (2024, 110)):
        for q in (1, 2, 3, 4):
            mex, usa = base + q, base // 2 + q
            rev = mex + usa
            cogs = rev * 0.6
            rows.append({"period": f"{year}-{q}T",
                         "revenue": rev, "revenue_mexico": mex, "revenue_usa": usa,
                         "cogs": cogs, "gross_profit": rev - cogs})
    return pd.DataFrame(rows)


REC_OUTLINE = """\
Revenues

Total
YoY
Mexico
YoY
USA
YoY

P&L

COGS
Gross Profit
Margin
"""

REC_SECTIONS = ["Revenues", "P&L"]
REC_MAPPING = {
    "Total": "revenue", "Mexico": "revenue_mexico", "USA": "revenue_usa",
    "COGS": "cogs", "Gross Profit": "gross_profit",
}


def _build(df, outline=REC_OUTLINE, sections=REC_SECTIONS, mapping=REC_MAPPING, **kw):
    rows = parse_outline(outline, sections, mapping)
    return build_outline_workbook("ACME: Co", rows, df,
                                  unit_map={k: "currency" for k in mapping.values()},
                                  **kw).active


def test_auto_segment_check_inserted(df_reconciling):
    ws = _build(df_reconciling)
    r = _find(ws, "Check")
    total = _find(ws, "Total")
    mex, usa = _find(ws, "Mexico"), _find(ws, "USA")
    assert ws[f"C{r}"].value == (
        f'=IF(COUNT(C{total},C{mex},C{usa})=3,'
        f'C{total}-SUM(C{mex},C{usa}),"N/A")'
    )


def test_auto_identity_check_inserted(df_reconciling):
    ws = _build(df_reconciling)
    r = _find(ws, "Check: Gross = Revenue − COGS")
    gp, rev, cogs = _find(ws, "Gross Profit"), _find(ws, "Total"), _find(ws, "COGS")
    assert ws[f"C{r}"].value == (
        f'=IF(COUNT(C{gp},C{rev},C{cogs})=3,'
        f'C{gp}-(C{rev}-C{cogs}),"N/A")'
    )


def test_auto_checks_kill_switch(df_reconciling):
    ws = _build(df_reconciling, auto_checks=False)
    labels = {ws[f"B{r}"].value for r in range(6, ws.max_row + 1)}
    assert "Check" not in labels
    assert not any(str(v).startswith("Check:") for v in labels if v)


def test_declared_check_not_duplicated(df_reconciling):
    outline = REC_OUTLINE.replace("USA\nYoY\n", "USA\nYoY\nCheck\n")
    ws = _build(df_reconciling, outline=outline)
    checks = [r for r in range(6, ws.max_row + 1) if ws[f"B{r}"].value == "Check"]
    assert len(checks) == 1


def test_non_reconciling_structure_keeps_visible_check(df):
    # A mismatch is exactly what the structural check is meant to expose.
    rows = parse_outline(OUTLINE, SECTIONS, MAPPING)
    ws = build_outline_workbook("ACME: Co", rows, df,
                                unit_map={"revenue": "currency"}).active
    labels = {ws[f"B{r}"].value for r in range(6, ws.max_row + 1)}
    assert "Check" in labels


def test_outline_workbook_fills_data(ws):
    r = _find(ws, "Total")
    assert ws[f"C{r}"].value == 101 and ws[f"H{r}"].value == 111
    assert ws[f"C{r}"].number_format == NUMFMT_MONEY
    assert ws[f"G{r}"].value == (
        f'=IF(COUNT(C{r}:F{r})=4,SUM(C{r}:F{r}),"N/A")'
    )
    # A populated keyed row renders its value (and is not pruned).
    rc = _find(ws, "Consolidated")                       # gross_profit, present in df
    assert ws[f"C{rc}"].value is not None


def test_outline_workbook_formulas(ws):
    r = _find(ws, "Mexico")                               # first Mexico = revenue_mexico
    yoy = r + 1
    assert ws[f"B{yoy}"].value == "YoY"
    assert ws[f"H{yoy}"].value == f'=IFERROR(H{r}/C{r}-1,"N/A")'
    pct = r + 2
    total_row = _find(ws, "Total")                        # global consolidated anchor
    assert ws[f"B{pct}"].value == "As % of Consolidated"
    assert ws[f"C{pct}"].value == f"=+C{r}/C${total_row}"
    assert ws[f"C{pct}"].number_format == NUMFMT_PCT


def test_margin_derived_is_formula(ws):
    r = _find(ws, "Margin")
    assert ws[f"B{r}"].value == "Margin"
    gp = _find(ws, "Consolidated")
    rev = _find(ws, "Total")
    assert ws[f"H{r}"].value == f'=IFERROR(H{gp}/H{rev},"N/A")'


def test_walmex_spec_parses_and_maps():
    import yaml
    spec = yaml.safe_load((ROOT / "configs" / "walmex_segments.yaml").read_text())
    rows = parse_outline(spec["outline"], spec["sections"], spec["mapping"])
    keys = {r.key for r in rows if r.key}
    for expected in ("revenue", "revenue_mexico", "revenue_cam",
                     "gross_profit_mexico", "ebitda_cam", "total_stores",
                     "sss_mexico", "sales_floor_mexico"):
        assert expected in keys, expected
    # repeated labels disambiguated
    mex = [r.key for r in rows if r.label == "Mexico" and r.kind == "data"]
    assert "gross_profit_mexico" in mex and "ebitda_mexico" in mex


# ---------------------------------------------------------------------------
# Derived calculations are LIVE formulas (not blank/hardcoded), and unreachable
# rows are pruned + reported by the audit.
# ---------------------------------------------------------------------------
from src.excel.segments_sheet import prune_outline, audit_outline   # noqa: E402

_CALC_OUTLINE = "\n".join([
    "P&L", "",
    "Total Income", "SSS YoY", "2-year comp",
    "Pre-tax Income", "Tax Provision", "Effective Tax Rate", "", "Net Income", "Margin", "",
    "Totals", "",
    "Total Units", "Total Sales Floor", "",
    "Hiper", "",
    "Hiper Units", "As % of Total", "Hiper Area", "Avg Store Size", "",
    "Productivity", "",
    "Sales per m²", "YoY Sales per m²", "Sales per Store", "",
    "Capex", "",
    "Total Capex", "Capex per Store", "Capex per m²",
])
_CALC_SECTIONS = ["P&L", "Totals", "Hiper", "Productivity", "Capex"]
_CALC_MAPPING = {
    "Total Income": "revenue", "SSS YoY": "sss", "Pre-tax Income": "ebt",
    "Tax Provision": "tax_expense", "Net Income": "net_income",
    "Total Units": "total_units", "Total Sales Floor": "total_sales_floor",
    "Hiper Units": "units_hiper", "Hiper Area": "area_hiper", "Total Capex": "capex",
}


def _calc_df():
    p = ["2024-1T", "2024-2T", "2025-1T", "2025-2T"]
    df = pd.DataFrame({"period": p, "revenue": [100, 110, 120, 130], "sss": [5, 6, 7, 8],
                       "ebt": [20, 22, 24, 26], "tax_expense": [6, 7, 8, 9],
                       "net_income": [14, 15, 16, 17], "total_units": [10, 10, 11, 11],
                       "total_sales_floor": [1000, 1000, 1100, 1100], "units_hiper": [4, 4, 5, 5],
                       "area_hiper": [400, 400, 500, 500], "capex": [50, 0, 55, 0]})
    df.attrs["confidence"] = {}
    return df


def _firstf(ws, r):
    for c in range(3, ws.max_column + 1):
        v = ws.cell(r, c).value
        if isinstance(v, str) and v.startswith("="):
            return v
    return None


def test_productivity_and_ratio_calcs_are_formulas():
    rows = parse_outline(_CALC_OUTLINE, _CALC_SECTIONS, _CALC_MAPPING)
    ws = build_outline_workbook("T", rows, _calc_df()).active
    found = {ws.cell(r, 2).value: _firstf(ws, r) for r in range(6, ws.max_row + 1)
             if ws.cell(r, 2).value}
    # every calculation row carries an =formula (no blanks / hardcoded values)
    assert found["Effective Tax Rate"].startswith("=IFERROR(") and "/" in found["Effective Tax Rate"]
    assert found["2-year comp"].startswith("=IFERROR((1+")          # stacked SSS comp
    assert found["Avg Store Size"].startswith("=IFERROR(")
    assert found["Sales per m²"].startswith("=IFERROR(") and found["Sales per Store"].startswith("=IFERROR(")
    assert found["Capex per Store"].startswith("=IFERROR(") and found["Capex per m²"].startswith("=IFERROR(")
    assert found["YoY Sales per m²"].startswith("=IFERROR(")        # YoY of the ratio row


def test_pct_of_total_targets_global_total_not_section_self():
    rows = parse_outline(_CALC_OUTLINE, _CALC_SECTIONS, _CALC_MAPPING)
    ws = build_outline_workbook("T", rows, _calc_df()).active
    total_units_row = next(r for r in range(6, ws.max_row + 1) if ws.cell(r, 2).value == "Total Units")
    pct_row = next(r for r in range(6, ws.max_row + 1) if ws.cell(r, 2).value == "As % of Total")
    # units_hiper / total_units — references the GLOBAL total, not the Hiper section's own row
    assert ws.cell(pct_row, 3).value == f"=+C{pct_row - 1}/C${total_units_row}"


def test_empty_rows_pruned_and_audited():
    outline = "\n".join(["P&L", "", "Total Income", "Mystery Metric", "Avg Ticket"])
    mapping = {"Total Income": "revenue", "Mystery Metric": "made_up_key"}
    rows = parse_outline(outline, ["P&L"], mapping)
    df = pd.DataFrame({"period": ["2024-1T"], "revenue": [100.0]})
    df.attrs["confidence"] = {}
    kept, dropped = prune_outline(rows, df)
    kept_labels = {r.label for r in kept}
    assert "Total Income" in kept_labels
    assert "Mystery Metric" not in kept_labels      # keyed but empty → pruned
    assert "Avg Ticket" not in kept_labels           # placeholder w/o key → pruned
    issues = {i["label"] for i in audit_outline(rows, df)}
    assert "Mystery Metric" in issues and "Avg Ticket" in issues


def test_segment_check_is_section_scoped():
    # consolidated − Σ(segments in THIS section); a same-family metric in another
    # section must NOT be summed in.
    outline = "\n".join(["Net Sales", "", "Consolidated Net Sales", "Domestic", "Export", "Check",
                         "", "Legacy", "", "Conservas — Net Sales"])
    mapping = {"Consolidated Net Sales": "revenue", "Domestic": "ns_domestic",
               "Export": "ns_export", "Conservas — Net Sales": "ns_conservas"}
    rows = parse_outline(outline, ["Net Sales", "Legacy"], mapping)
    df = pd.DataFrame({"period": ["2024-1T"], "revenue": [100.0], "ns_domestic": [80.0],
                       "ns_export": [20.0], "ns_conservas": [80.0]})
    df.attrs["confidence"] = {}
    ws = build_outline_workbook("T", rows, df).active
    check_row = next(r for r in range(6, ws.max_row + 1) if ws.cell(r, 2).value == "Check")
    f = ws.cell(check_row, 3).value
    # ns_conservas (Legacy section, also ns_ family) is excluded
    total = _find(ws, "Consolidated Net Sales")
    domestic, export = _find(ws, "Domestic"), _find(ws, "Export")
    conservas = _find(ws, "Conservas — Net Sales")
    assert f == (f'=IF(COUNT(C{total},C{domestic},C{export})=3,'
                 f'C{total}-SUM(C{domestic},C{export}),"N/A")')
    assert f"C{conservas}" not in f


def test_fy_aggregation_distinguishes_flows_stocks_and_average():
    rows = [
        RowSpec("section", "Metrics"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("data", "Cash", key="cash"),
        RowSpec("data", "Average Utilization", key="utilization"),
    ]
    df = pd.DataFrame({
        "period": ["2024-1T", "2024-2T", "2024-3T", "2024-4T"],
        "revenue": [10.0, 11.0, 12.0, 13.0],
        "cash": [100.0, 110.0, 120.0, 130.0],
        "utilization": [0.7, 0.8, 0.9, 1.0],
    })
    layout = MetricLayoutMap(
        {"revenue": "currency", "cash": "currency", "utilization": "pct"},
        aggregations={"revenue": "sum", "cash": "ending", "utilization": "average"},
    )
    ws = build_outline_workbook("T", rows, df, unit_map=layout,
                                auto_checks=False).active
    revenue, cash, utilization = (_find(ws, label)
                                  for label in ("Revenue", "Cash", "Average Utilization"))
    assert ws[f"G{revenue}"].value == (
        f'=IF(COUNT(C{revenue}:F{revenue})=4,SUM(C{revenue}:F{revenue}),"N/A")'
    )
    assert ws[f"G{cash}"].value == f'=IF(COUNT(F{cash})=1,F{cash},"N/A")'
    assert ws[f"G{utilization}"].value == (
        f'=IF(COUNT(C{utilization}:F{utilization})=4,'
        f'AVERAGE(C{utilization}:F{utilization}),"N/A")'
    )


def test_calculated_sources_compile_but_reported_sources_stay_inputs():
    rows = [
        RowSpec("section", "Balance Sheet"),
        RowSpec("data", "Short-Term Debt", key="short_term_debt"),
        RowSpec("data", "Long-Term Debt", key="long_term_debt"),
        RowSpec("data", "Total Debt", key="total_debt"),
        RowSpec("data", "Cash", key="cash"),
        RowSpec("data", "Net Debt", key="net_debt"),
        RowSpec("section", "P&L"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("data", "COGS", key="cogs"),
        RowSpec("data", "Reported Gross Profit", key="gross_profit"),
    ]
    df = pd.DataFrame({
        "period": ["2024-1T"],
        "short_term_debt": [20.0], "long_term_debt": [80.0],
        "total_debt": [100.0], "cash": [25.0], "net_debt": [75.0],
        "revenue": [200.0], "cogs": [120.0], "gross_profit": [80.0],
    })
    df.attrs["confidence"] = {
        ("2024-1T", "total_debt"): {
            "source": "[statement] gmexico consolidated Deuda corto + largo plazo",
        },
        ("2024-1T", "net_debt"): {
            "source": "[statement] gmexico consolidated total_debt − cash",
        },
        ("2024-1T", "gross_profit"): {
            "source": "[statement] Utilidad bruta",
        },
    }
    layout = MetricLayoutMap(
        {key: "currency" for key in df.columns if key != "period"},
        calculations={
            "total_debt": "short_term_debt + long_term_debt",
            "net_debt": "total_debt - cash",
            "gross_profit": "revenue - cogs",
        },
    )
    ws = build_outline_workbook("T", rows, df, unit_map=layout,
                                auto_checks=False).active
    total_debt, net_debt = _find(ws, "Total Debt"), _find(ws, "Net Debt")
    gross_profit = _find(ws, "Reported Gross Profit")
    assert str(ws[f"C{total_debt}"].value).startswith("=IF(COUNT(")
    assert str(ws[f"C{net_debt}"].value).startswith("=IF(COUNT(")
    assert ws[f"C{gross_profit}"].value == 80.0


def test_preserve_requested_rows_keeps_missing_and_formula_capable_rows():
    rows = [
        RowSpec("section", "Requested P&L"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("data", "COGS", key="cogs"),
        RowSpec("data", "Gross Profit", key="gross_profit"),
        RowSpec("section", "Requested KPI"),
        RowSpec("data", "Analyst-only Metric", key="analyst_only"),
    ]
    df = pd.DataFrame({"period": ["2024-1T"], "revenue": [100.0], "cogs": [60.0]})
    layout = MetricLayoutMap(
        {"revenue": "currency", "cogs": "currency", "gross_profit": "currency",
         "analyst_only": "currency"},
        calculations={"gross_profit": "revenue - cogs"},
    )
    ws = build_outline_workbook(
        "T", rows, df, unit_map=layout, auto_checks=False,
        preserve_requested_rows=True,
    ).active
    labels = [ws.cell(r, 2).value for r in range(6, ws.max_row + 1)]
    assert labels == [
        "Requested P&L", "Revenue", "COGS", "Gross Profit",
        "Requested KPI", "Analyst-only Metric",
    ]
    revenue, cogs = _find(ws, "Revenue"), _find(ws, "COGS")
    gross_profit, missing = _find(ws, "Gross Profit"), _find(ws, "Analyst-only Metric")
    assert ws[f"C{gross_profit}"].value == (
        f'=IF(COUNT(C{revenue},C{cogs})=2,'
        f'IFERROR((C{revenue}-C{cogs}),"N/A"),"N/A")'
    )
    assert ws[f"C{missing}"].value is None
    assert ws[f"C{missing}"].fill.fgColor.rgb == "FFF8CBAD"
    issues = audit_outline(
        rows, df, unit_map=layout, preserve_requested_rows=True,
    )
    assert {issue["label"] for issue in issues} == {"Analyst-only Metric"}


def test_delta_metric_is_live_period_over_period_formula():
    rows = [
        RowSpec("section", "Stores"),
        RowSpec("data", "Total Units", key="total_units"),
        RowSpec("data", "Net New Stores", key="net_new_stores"),
    ]
    df = pd.DataFrame({
        "period": ["2023-4T", "2024-1T"],
        "total_units": [100.0, 103.0],
        "net_new_stores": [float("nan"), 3.0],
    })
    layout = MetricLayoutMap(
        {"total_units": "count", "net_new_stores": "count"},
        aggregations={"total_units": "ending", "net_new_stores": "sum"},
        delta_metrics={"net_new_stores": "total_units"},
    )
    ws = build_outline_workbook("T", rows, df, unit_map=layout,
                                auto_checks=False).active
    units, delta = _find(ws, "Total Units"), _find(ws, "Net New Stores")
    assert ws[f"H{delta}"].value == (
        f'=IF(COUNT(H{units},F{units})=2,H{units}-F{units},"N/A")'
    )
    assert ws[f"L{delta}"].value == (
        f'=IF(COUNT(H{delta}:K{delta})=4,SUM(H{delta}:K{delta}),"N/A")'
    )


def test_net_debt_and_fcf_checks_are_structural_and_skippable():
    rows = [
        RowSpec("section", "Leverage"),
        RowSpec("data", "Cash", key="cash"),
        RowSpec("data", "Total Debt", key="total_debt"),
        RowSpec("data", "Net Debt", key="net_debt"),
        RowSpec("section", "Cash Flow"),
        RowSpec("data", "CFO", key="cfo"),
        RowSpec("data", "Capex", key="capex"),
        RowSpec("data", "FCF", key="free_cash_flow"),
    ]
    # Both identities intentionally disagree: the rows must still be visible.
    df = pd.DataFrame({
        "period": ["2024-1T"], "cash": [25.0], "total_debt": [100.0],
        "net_debt": [999.0], "cfo": [50.0], "capex": [10.0],
        "free_cash_flow": [999.0],
    })
    layout = MetricLayoutMap(
        {key: "currency" for key in df.columns if key != "period"},
        skip_rules={"fcf_derivation"},
    )
    ws = build_outline_workbook("T", rows, df, unit_map=layout).active
    labels = {ws.cell(r, 2).value for r in range(6, ws.max_row + 1)}
    assert "Check: Net debt = Total debt − Cash" in labels
    assert "Check: FCF = CFO − Capex" not in labels
    check = _find(ws, "Check: Net debt = Total debt − Cash")
    assert ws[f"C{check}"].value.startswith("=IF(COUNT(")


def test_strict_audit_flags_calc_operands_with_no_period_overlap():
    rows = [
        RowSpec("section", "P&L"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("data", "COGS", key="cogs"),
        RowSpec("data", "Gross Profit", key="gross_profit"),
    ]
    df = pd.DataFrame({
        "period": ["2024-1T", "2024-2T"],
        "revenue": [100.0, float("nan")],
        "cogs": [float("nan"), 60.0],
    })
    layout = MetricLayoutMap(
        {"revenue": "currency", "cogs": "currency", "gross_profit": "currency"},
        calculations={"gross_profit": "revenue - cogs"},
    )
    issues = audit_outline(
        rows, df, unit_map=layout, preserve_requested_rows=True,
    )
    assert [(issue["label"], issue["reason"]) for issue in issues] == [
        ("Gross Profit",
         "calculated metric 'gross_profit' has zero evaluable periods "
         "(required operands never overlap)"),
    ]


def test_strict_audit_requires_same_quarter_prior_for_yoy_and_overlap_for_margin():
    rows = [
        RowSpec("section", "P&L"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("derived", "YoY", derived="yoy"),
        RowSpec("data", "EBITDA", key="ebitda"),
        RowSpec("derived", "Margin", derived="margin"),
    ]
    df = pd.DataFrame({
        "period": ["2023-1T", "2023-2T", "2024-1T", "2024-2T"],
        "revenue": [100.0, float("nan"), float("nan"), 110.0],
        "ebitda": [float("nan"), 40.0, 45.0, float("nan")],
    })
    layout = MetricLayoutMap({"revenue": "currency", "ebitda": "currency"})
    issues = audit_outline(
        rows, df, unit_map=layout, preserve_requested_rows=True,
    )
    assert {issue["label"] for issue in issues} == {"YoY", "Margin"}
    assert all("zero evaluable periods" in issue["reason"] for issue in issues)


def test_strict_audit_accepts_at_least_one_evaluable_formula_period():
    rows = [
        RowSpec("section", "P&L"),
        RowSpec("data", "Revenue", key="revenue"),
        RowSpec("derived", "YoY", derived="yoy"),
        RowSpec("data", "EBITDA", key="ebitda"),
        RowSpec("derived", "Margin", derived="margin"),
    ]
    df = pd.DataFrame({
        "period": ["2023-1T", "2024-1T"],
        "revenue": [100.0, 110.0],
        "ebitda": [float("nan"), 45.0],
    })
    layout = MetricLayoutMap({"revenue": "currency", "ebitda": "currency"})
    assert audit_outline(
        rows, df, unit_map=layout, preserve_requested_rows=True,
    ) == []


def test_latest_unreported_quarters_are_hidden_not_marked_as_missing_data():
    rows = [RowSpec("section", "P&L"), RowSpec("data", "Revenue", key="revenue")]
    df = pd.DataFrame({
        "period": [
            "2025-1T", "2025-2T", "2025-3T", "2025-4T",
            "2026-1T", "2026-2T",
        ],
        "revenue": [10.0, 11.0, 12.0, 13.0, 14.0, 15.0],
    })
    ws = build_outline_workbook(
        "T",
        rows,
        df,
        unit_map=MetricLayoutMap({"revenue": "currency"}),
        auto_checks=False,
    ).active

    assert ws.column_dimensions["H"].hidden is False  # 1Q26
    assert ws.column_dimensions["I"].hidden is False  # 2Q26
    assert ws.column_dimensions["J"].hidden is True   # 3Q26, not reported
    assert ws.column_dimensions["K"].hidden is True   # 4Q26, not reported
    assert ws.column_dimensions["L"].hidden is True   # FY26, incomplete
