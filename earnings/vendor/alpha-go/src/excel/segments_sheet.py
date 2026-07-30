"""
segments_sheet.py — Build the model's standardized ``Segments`` Excel sheet.

Given a user-defined metric list and the values produced by the existing
download + extraction pipeline (``pipeline.run``), this lays the segment data
into the house format used in ``/style/*.xlsx`` and fills the hard inputs,
leaving every derived number (YoY, "As % of Consolidated", FY totals and the
consolidated total) as an Excel formula so the workbook stays live.

Layout (reverse-engineered from the reference files):

    A2  Title, e.g. "BIMBO: Grupo BIMBO"          Arial 12 bold
    A3  "Segment data"                            Arial 10 bold
    B5  "Segments (in P$mn)"  | C5..L5 periods     Arial 10 bold, bottom border
    B6  "Revenues" (section header)               Arial 12 bold, bottom border

    Columns:  C D E F = prior-year Q1..Q4,  G = prior FY,
              H I J K = current-year Q1..Q4, L = current FY.

    Row 8  consolidated total (= sum of segments) + row 9 YoY, then one
    block per segment: data row, YoY row, "As % of Consolidated" row.

The metric list names ONLY the segment data rows; all derived rows are
generated automatically. Metric keys not present in the extracted data render
as blank cells (the formulas degrade gracefully via IFERROR / SUM).

Typical use::

    from src.excel.segments_sheet import SegmentRow, generate_segments_sheet
    segments = [
        SegmentRow("Net Sales North America", "net_sales_north_america"),
        SegmentRow("Net Sales Mexico",        "net_sales_mexico"),
    ]
    wb = generate_segments_sheet("./reportes", segments,
                                 config="configs/bimbo.yaml", out_path="seg.xlsx")
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Color, Font, PatternFill, Side
from openpyxl.worksheet.worksheet import Worksheet

# ---------------------------------------------------------------------------
# Style constants (kept here so the look is easy to tweak in one place)
# ---------------------------------------------------------------------------
_FONT = "Arial"
_BLUE = "FF1F497D"          # hard-input value font color
_GREY = "FF54585A"          # derived-row (YoY / %) font color

FONT_TITLE   = Font(name=_FONT, size=12, bold=True)
FONT_SUB     = Font(name=_FONT, size=10, bold=True)
FONT_SECTION = Font(name=_FONT, size=12, bold=True)
FONT_HEADER  = Font(name=_FONT, size=10, bold=True)
FONT_LABEL   = Font(name=_FONT, size=9, bold=True)
FONT_VALUE   = Font(name=_FONT, size=9, bold=True, color=_BLUE)
FONT_DERIVED = Font(name=_FONT, size=9, italic=True, color=_GREY)

# Light-grey banding on data rows (theme background, slightly darkened).
FILL_DATA = PatternFill(patternType="solid",
                        fgColor=Color(theme=0, tint=-0.0499893185216834))

BORDER_BOTTOM = Border(bottom=Side(style="thin"))

NUMFMT_MONEY = '"$"\\ #,##0.0;[Red]\\("$"\\ #,##0.0\\)'
NUMFMT_PCT   = '#,##0.0%;[Red]\\(#,##0.0%\\)'
NUMFMT_NUMBER = '#,##0.0;[Red]\\(#,##0.0\\)'
NUMFMT_BPS = '#,##0;[Red]\\(#,##0\\)'

ALIGN_LEFT  = Alignment(horizontal="left")
ALIGN_RIGHT = Alignment(horizontal="right")

# Fixed grid geometry.
_PRIOR_Q = {1: "C", 2: "D", 3: "E", 4: "F"}   # prior-year quarter columns
_PRIOR_FY = "G"
_CURR_Q  = {1: "H", 2: "I", 3: "J", 4: "K"}   # current-year quarter columns
_CURR_FY = "L"
_VALUE_COLS = list("CDEFGHIJKL")              # every numeric column, left→right
_HEADER_ROW = 5
_SECTION_ROW = 6
_CONS_ROW = 8                                 # consolidated total row
_FIRST_SEG_ROW = 11                           # first segment data row
_BLOCK = 3                                    # rows per segment: data, YoY, %

_PERIOD_RE = re.compile(r"(\d{4})-(\d)[TQ]", re.IGNORECASE)


@dataclass
class SegmentRow:
    """One entry of the metric list: the B-column label + the extractor key."""
    label: str
    key: str


@dataclass
class RowSpec:
    """One laid-out row of an outline-driven sheet.

    ``kind``     — one of ``section`` / ``data`` / ``derived`` / ``spacer``.
    ``key``      — extractor metric key for ``data`` rows (``None`` → blank row).
    ``derived``  — for ``derived`` rows: ``yoy`` / ``pct_consolidated`` /
                   ``pct_total`` / ``plain`` (``plain`` = label-only placeholder).
    """
    kind: str
    label: str
    key: str | None = None
    derived: str | None = None


# Derived-row labels that carry NO data (formulas or placeholders). Anything
# matching is never extracted — honoring "ignore computed vars: yoy, check, …".
_DERIVED_EXACT = {
    "yoy": "yoy",
    "check": "check",
    "margin": "margin",
    "bps change": "bps_change",
    "fx effect": "blank_note",
    "2-year comp": "plain",
    "new store utilization": "plain",
}


def _classify_derived(label: str) -> str | None:
    """Return the derived subtype for a label, or ``None`` if it is not derived."""
    t = label.strip().lower()
    if t.startswith("as % of total"):
        return "pct_total"
    if t.startswith("as % of"):
        return "pct_consolidated"
    if t.startswith("average price"):
        return "average_price"
    if t.endswith(" margin"):
        return "margin"
    return _DERIVED_EXACT.get(t)


def _next_nonblank(lines: list[str], i: int) -> str | None:
    for j in range(i + 1, len(lines)):
        if lines[j]:
            return lines[j]
    return None


def parse_outline(text: str, sections=(), mapping=None) -> list[RowSpec]:
    """Turn a pasted metric outline into ordered ``RowSpec``s.

    Classification is layered so a label that is a *section* in one place and a
    *data row* in another (e.g. "Mexico") resolves correctly:
      1. blank line                                   → spacer
      2. derived keyword / "As % of …"                → derived
      3. label is in ``mapping`` (``Section/Label`` or
         plain ``Label``)                             → data (with key)
      4. next non-blank line is a derived row          → data (blank)
      5. label is in the explicit ``sections`` set     → section
      6. otherwise                                     → data (blank)

    ``mapping`` keys may be ``"Section/Label"`` to disambiguate repeated
    sub-labels, or ``"Previous data label/Label"`` for repeated child rows
    such as ``Volume`` under several segment sales rows.
    """
    mapping = mapping or {}
    section_set = {s.strip() for s in sections}
    lines = [ln.strip() for ln in text.splitlines()]

    rows: list[RowSpec] = []
    cur_section = ""
    prev_data_label = ""
    for i, label in enumerate(lines):
        if not label:
            rows.append(RowSpec("spacer", ""))
            continue
        d = _classify_derived(label)
        if d is not None:
            rows.append(RowSpec("derived", label, derived=d))
            continue
        key = (
            mapping.get(f"{cur_section}/{label}")
            or mapping.get(f"{prev_data_label}/{label}")
            or mapping.get(label)
        )
        if key:
            rows.append(RowSpec("data", label, key=key))
            prev_data_label = label
            continue
        nxt = _next_nonblank(lines, i)
        if nxt is not None and _classify_derived(nxt) is not None:
            rows.append(RowSpec("data", label))
            prev_data_label = label
            continue
        if label in section_set:
            cur_section = label
            prev_data_label = ""
            rows.append(RowSpec("section", label))
            continue
        rows.append(RowSpec("data", label))
        prev_data_label = label
    return rows


# ---------------------------------------------------------------------------
# Data plumbing
# ---------------------------------------------------------------------------
def _value_lookup(df) -> dict:
    """Build ``{(year, quarter): {metric_key: value}}`` from a wide DataFrame.

    The DataFrame is the output of ``pipeline.run``: one row per period
    (``YYYY-QT``), columns = metric keys. Missing / NaN values are skipped.
    """
    import math

    table: dict = {}
    if df is None or len(df) == 0 or "period" not in df.columns:
        return table
    keys = [c for c in df.columns if c != "period"]
    for _, row in df.iterrows():
        m = _PERIOD_RE.match(str(row["period"]))
        if not m:
            continue
        year, quarter = int(m.group(1)), int(m.group(2))
        cell: dict = {}
        for k in keys:
            v = row[k]
            if v is None:
                continue
            try:
                if isinstance(v, float) and math.isnan(v):
                    continue
            except (TypeError, ValueError):
                pass
            cell[k] = v
        table[(year, quarter)] = cell
    return table


def _pick_years(df, years: tuple[int, int] | None) -> tuple[int, int]:
    """Return ``(prior_year, current_year)`` — explicit override or latest two."""
    if years:
        return years
    found: set[int] = set()
    if df is not None and "period" in getattr(df, "columns", []):
        for p in df["period"]:
            m = _PERIOD_RE.match(str(p))
            if m:
                found.add(int(m.group(1)))
    if not found:
        return (0, 0)
    current = max(found)
    return (current - 1, current)


# ---------------------------------------------------------------------------
# Cell helpers
# ---------------------------------------------------------------------------
def _put(ws: Worksheet, ref: str, value, *, font, numfmt=None,
         align=ALIGN_RIGHT, fill=None, border=None) -> None:
    cell = ws[ref]
    cell.value = value
    cell.font = font
    cell.alignment = align
    if numfmt:
        cell.number_format = numfmt
    if fill is not None:
        cell.fill = fill
    if border is not None:
        cell.border = border


def _yoy_formula(col: str, row: int) -> str:
    """Current-quarter column → same quarter prior year (H→C, I→D, … L→G)."""
    prior = {"H": "C", "I": "D", "J": "E", "K": "F", "L": "G"}[col]
    return f'=IFERROR({col}{row}/{prior}{row}-1,"N/A")'


def _bps_formula(col: str, row: int) -> str:
    prior = {"H": "C", "I": "D", "J": "E", "K": "F", "L": "G"}[col]
    return f'=IFERROR(({col}{row}-{prior}{row})*10000,"N/A")'


def _numfmt_for_unit(unit: str | None) -> str:
    if unit == "pct":
        return NUMFMT_PCT
    if unit in {"count", "volume"}:
        return NUMFMT_NUMBER
    return NUMFMT_MONEY


def _is_fy_summable(unit: str | None) -> bool:
    return unit in {"currency", "volume"}


def _metric_family(key: str | None) -> str | None:
    if not key:
        return None
    if key == "revenue" or key.startswith("net_sales_"):
        return "revenue"
    for family in ("volume", "gross_profit", "operating_income", "ebitda"):
        if key == family or key.startswith(f"{family}_"):
            return family
    return None


def _metric_suffix(key: str | None) -> str:
    if not key:
        return ""
    if key == "revenue":
        return ""
    for prefix in ("net_sales", "volume", "gross_profit", "operating_income", "ebitda"):
        if key == prefix:
            return ""
        marker = f"{prefix}_"
        if key.startswith(marker):
            return key[len(marker):]
    return ""


def _consolidated_key_for_family(family: str | None) -> str | None:
    if family == "revenue":
        return "revenue"
    return family


def _sales_key_for_suffix(suffix: str) -> str:
    return f"net_sales_{suffix}" if suffix else "revenue"


def _volume_key_for_suffix(suffix: str) -> str:
    return f"volume_{suffix}" if suffix else "volume"


# ---------------------------------------------------------------------------
# Sheet builder
# ---------------------------------------------------------------------------
def build_segments_workbook(
    title: str,
    segments: list[SegmentRow],
    df,
    *,
    section: str = "Revenues",
    consolidated_label: str = "Consolidated",
    units: str = "P$mn",
    years: tuple[int, int] | None = None,
) -> Workbook:
    """Build a workbook with a single, fully formatted ``Segments`` sheet."""
    prior_year, current_year = _pick_years(df, years)
    lookup = _value_lookup(df)

    wb = Workbook()
    ws = wb.active
    ws.title = "Segments"
    ws.sheet_view.showGridLines = False

    # Column widths.
    ws.column_dimensions["A"].width = 1.5
    ws.column_dimensions["B"].width = 43.1640625
    for col in _VALUE_COLS:
        ws.column_dimensions[col].width = 11.6640625

    # Title block.
    _put(ws, "A2", title, font=FONT_TITLE, align=ALIGN_LEFT)
    _put(ws, "A3", "Segment data", font=FONT_SUB, align=ALIGN_LEFT)

    # Period header row.
    _put(ws, f"B{_HEADER_ROW}", f"Segments (in {units})",
         font=FONT_HEADER, align=ALIGN_LEFT, border=BORDER_BOTTOM)
    for q, col in _PRIOR_Q.items():
        _put(ws, f"{col}{_HEADER_ROW}", f"{q}Q{prior_year % 100:02d}A",
             font=FONT_HEADER, border=BORDER_BOTTOM)
    _put(ws, f"{_PRIOR_FY}{_HEADER_ROW}", f"FY{prior_year % 100:02d}A",
         font=FONT_HEADER, border=BORDER_BOTTOM)
    for q, col in _CURR_Q.items():
        _put(ws, f"{col}{_HEADER_ROW}", f"{q}Q{current_year % 100:02d}A",
             font=FONT_HEADER, border=BORDER_BOTTOM)
    _put(ws, f"{_CURR_FY}{_HEADER_ROW}", f"FY{current_year % 100:02d}A",
         font=FONT_HEADER, border=BORDER_BOTTOM)

    # Section header.
    _put(ws, f"B{_SECTION_ROW}", section,
         font=FONT_SECTION, align=ALIGN_LEFT, border=BORDER_BOTTOM)

    # Segment data row numbers (deterministic, two-pass-friendly).
    seg_rows = [_FIRST_SEG_ROW + i * _BLOCK for i in range(len(segments))]

    # --- Consolidated total (sum of segment rows) + its YoY ----------------
    _put(ws, f"B{_CONS_ROW}", consolidated_label,
         font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
    for col in ("C", "D", "E", "F", "H", "I", "J", "K"):
        formula = "=" + "+".join(f"{col}{r}" for r in seg_rows) if seg_rows else None
        _put(ws, f"{col}{_CONS_ROW}", formula,
             font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
    _put(ws, f"{_PRIOR_FY}{_CONS_ROW}", f"=SUM(C{_CONS_ROW}:F{_CONS_ROW})",
         font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
    _put(ws, f"{_CURR_FY}{_CONS_ROW}", f"=SUM(H{_CONS_ROW}:K{_CONS_ROW})",
         font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)

    yoy_row = _CONS_ROW + 1
    _put(ws, f"B{yoy_row}", "YoY", font=FONT_DERIVED, align=ALIGN_LEFT)
    for col in ("H", "I", "J", "K", "L"):
        _put(ws, f"{col}{yoy_row}", _yoy_formula(col, _CONS_ROW),
             font=FONT_DERIVED, numfmt=NUMFMT_PCT)

    # --- One block per segment --------------------------------------------
    for seg, data_row in zip(segments, seg_rows):
        # Data row — fill hard inputs from the extractor.
        _put(ws, f"B{data_row}", seg.label,
             font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
        for q, col in _PRIOR_Q.items():
            v = lookup.get((prior_year, q), {}).get(seg.key)
            _put(ws, f"{col}{data_row}", v,
                 font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
        for q, col in _CURR_Q.items():
            v = lookup.get((current_year, q), {}).get(seg.key)
            _put(ws, f"{col}{data_row}", v,
                 font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
        _put(ws, f"{_PRIOR_FY}{data_row}", f"=SUM(C{data_row}:F{data_row})",
             font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
        _put(ws, f"{_CURR_FY}{data_row}", f"=SUM(H{data_row}:K{data_row})",
             font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)

        # YoY row.
        r_yoy = data_row + 1
        _put(ws, f"B{r_yoy}", "YoY", font=FONT_DERIVED, align=ALIGN_LEFT)
        for col in ("H", "I", "J", "K", "L"):
            _put(ws, f"{col}{r_yoy}", _yoy_formula(col, data_row),
                 font=FONT_DERIVED, numfmt=NUMFMT_PCT)

        # As % of Consolidated row.
        r_pct = data_row + 2
        _put(ws, f"B{r_pct}", "As % of Consolidated",
             font=FONT_DERIVED, align=ALIGN_LEFT)
        for col in _VALUE_COLS:
            _put(ws, f"{col}{r_pct}", f"=+{col}{data_row}/{col}${_CONS_ROW}",
                 font=FONT_DERIVED, numfmt=NUMFMT_PCT)

    ws.freeze_panes = "C8"
    return wb


def _write_header(ws, units: str, prior_year: int, current_year: int) -> None:
    """Write the title-block period header row (row 5), shared by both builders."""
    _put(ws, f"B{_HEADER_ROW}", f"Segments (in {units})",
         font=FONT_HEADER, align=ALIGN_LEFT, border=BORDER_BOTTOM)
    for q, col in _PRIOR_Q.items():
        _put(ws, f"{col}{_HEADER_ROW}", f"{q}Q{prior_year % 100:02d}A",
             font=FONT_HEADER, border=BORDER_BOTTOM)
    _put(ws, f"{_PRIOR_FY}{_HEADER_ROW}", f"FY{prior_year % 100:02d}A",
         font=FONT_HEADER, border=BORDER_BOTTOM)
    for q, col in _CURR_Q.items():
        _put(ws, f"{col}{_HEADER_ROW}", f"{q}Q{current_year % 100:02d}A",
             font=FONT_HEADER, border=BORDER_BOTTOM)
    _put(ws, f"{_CURR_FY}{_HEADER_ROW}", f"FY{current_year % 100:02d}A",
         font=FONT_HEADER, border=BORDER_BOTTOM)


def build_outline_workbook(
    title: str,
    rows: list[RowSpec],
    df,
    *,
    units: str = "P$mn",
    unit_map: dict | None = None,
    years: tuple[int, int] | None = None,
    subtitle: str = "Segment data",
) -> Workbook:
    """Lay out an arbitrary outline verbatim into the standardized Segments sheet.

    ``rows`` come from :func:`parse_outline`. Data rows are filled from ``df``
    (blank when the key is absent / not extracted); ``YoY`` and ``As % of …``
    rows become Excel formulas; other derived rows (``Margin``, ``bps change``,
    ``Check`` …) are label-only placeholders. FY ``=SUM`` columns are written
    only for currency-unit metrics (summing stocks / percentages is meaningless).
    """
    prior_year, current_year = _pick_years(df, years)
    lookup = _value_lookup(df)
    unit_map = unit_map or {}

    wb = Workbook()
    ws = wb.active
    ws.title = "Segments"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 1.5
    ws.column_dimensions["B"].width = 43.1640625
    for col in _VALUE_COLS:
        ws.column_dimensions[col].width = 11.6640625

    _put(ws, "A2", title, font=FONT_TITLE, align=ALIGN_LEFT)
    _put(ws, "A3", subtitle, font=FONT_SUB, align=ALIGN_LEFT)
    _write_header(ws, units, prior_year, current_year)

    r = _HEADER_ROW + 1
    first_data_row: int | None = None    # fallback global consolidated anchor
    section_anchor: int | None = None    # first data row in current section (% of Total)
    last_data_row: int | None = None     # nearest hard-input data row above
    last_data_key: str | None = None
    last_yoy_anchor: int | None = None
    last_margin_row: int | None = None
    data_row_by_key: dict[str, int] = {}

    for spec in rows:
        if spec.kind == "spacer":
            r += 1
            continue

        if spec.kind == "section":
            _put(ws, f"B{r}", spec.label,
                 font=FONT_SECTION, align=ALIGN_LEFT, border=BORDER_BOTTOM)
            section_anchor = None
            last_margin_row = None
            r += 1
            continue

        if spec.kind == "data":
            unit = unit_map.get(spec.key, "currency") if spec.key else None
            is_summable = bool(spec.key) and _is_fy_summable(unit)
            numfmt = _numfmt_for_unit(unit)
            _put(ws, f"B{r}", spec.label,
                 font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
            for q, col in _PRIOR_Q.items():
                v = lookup.get((prior_year, q), {}).get(spec.key) if spec.key else None
                _put(ws, f"{col}{r}", v, font=FONT_VALUE, numfmt=numfmt, fill=FILL_DATA)
            for q, col in _CURR_Q.items():
                v = lookup.get((current_year, q), {}).get(spec.key) if spec.key else None
                _put(ws, f"{col}{r}", v, font=FONT_VALUE, numfmt=numfmt, fill=FILL_DATA)
            _put(ws, f"{_PRIOR_FY}{r}", (f"=SUM(C{r}:F{r})" if is_summable else None),
                 font=FONT_VALUE, numfmt=numfmt, fill=FILL_DATA)
            _put(ws, f"{_CURR_FY}{r}", (f"=SUM(H{r}:K{r})" if is_summable else None),
                 font=FONT_VALUE, numfmt=numfmt, fill=FILL_DATA)
            last_data_row = r
            last_data_key = spec.key
            last_yoy_anchor = r
            if first_data_row is None:
                first_data_row = r
            if section_anchor is None:
                section_anchor = r
            if spec.key:
                data_row_by_key[spec.key] = r
            r += 1
            continue

        if spec.kind == "derived":
            _put(ws, f"B{r}", spec.label, font=FONT_DERIVED, align=ALIGN_LEFT)
            if spec.derived == "yoy" and last_yoy_anchor:
                for col in ("H", "I", "J", "K", "L"):
                    _put(ws, f"{col}{r}", _yoy_formula(col, last_yoy_anchor),
                         font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "pct_consolidated" and last_data_row:
                family = _metric_family(last_data_key)
                anchor_key = _consolidated_key_for_family(family)
                anchor_row = data_row_by_key.get(anchor_key or "") or first_data_row
                if anchor_row:
                    for col in _VALUE_COLS:
                        _put(ws, f"{col}{r}", f"=+{col}{last_data_row}/{col}${anchor_row}",
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "pct_total" and last_data_row and section_anchor:
                for col in _VALUE_COLS:
                    _put(ws, f"{col}{r}", f"=+{col}{last_data_row}/{col}${section_anchor}",
                         font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "average_price" and last_data_key:
                suffix = _metric_suffix(last_data_key)
                sales_row = data_row_by_key.get(_sales_key_for_suffix(suffix))
                volume_row = data_row_by_key.get(_volume_key_for_suffix(suffix))
                if sales_row and volume_row:
                    for col in _VALUE_COLS:
                        _put(ws, f"{col}{r}", f'=IFERROR({col}{sales_row}/{col}{volume_row},"N/A")',
                             font=FONT_DERIVED, numfmt=NUMFMT_MONEY)
                    last_yoy_anchor = r
            elif spec.derived == "margin" and last_data_row and last_data_key:
                last_margin_row = None
                sales_row = data_row_by_key.get(_sales_key_for_suffix(_metric_suffix(last_data_key)))
                if sales_row:
                    for col in _VALUE_COLS:
                        _put(ws, f"{col}{r}", f'=IFERROR({col}{last_data_row}/{col}{sales_row},"N/A")',
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
                    last_margin_row = r
            elif spec.derived == "bps_change" and last_margin_row:
                for col in ("H", "I", "J", "K", "L"):
                    _put(ws, f"{col}{r}", _bps_formula(col, last_margin_row),
                         font=FONT_DERIVED, numfmt=NUMFMT_BPS)
            elif spec.derived == "check" and last_data_key:
                family = _metric_family(last_data_key)
                anchor_key = _consolidated_key_for_family(family)
                anchor_row = data_row_by_key.get(anchor_key or "")
                segment_rows = [
                    row for key, row in data_row_by_key.items()
                    if key != anchor_key and _metric_family(key) == family
                ]
                if anchor_row and segment_rows:
                    for col in _VALUE_COLS:
                        formula = f"={col}{anchor_row}-SUM(" + ",".join(f"{col}{row}" for row in segment_rows) + ")"
                        _put(ws, f"{col}{r}", formula, font=FONT_DERIVED,
                             numfmt=_numfmt_for_unit(unit_map.get(anchor_key or "", "currency")))
            elif spec.derived == "blank_note":
                ws[f"B{r}"].comment = Comment(
                    "Metric unavailable in the source table; left blank intentionally.",
                    "Codex",
                )
            r += 1
            continue

    ws.freeze_panes = "C7"
    return wb


# ---------------------------------------------------------------------------
# Orchestration: download + extract + build
# ---------------------------------------------------------------------------
def generate_segments_sheet(
    source,
    segments: list[SegmentRow],
    *,
    config=None,
    title: str | None = None,
    out_path=None,
    section: str = "Revenues",
    consolidated_label: str = "Consolidated",
    units: str = "P$mn",
    years: tuple[int, int] | None = None,
    **run_kwargs,
) -> Workbook:
    """Run the existing pipeline for ``segments`` then build the Segments sheet.

    ``source``/``config``/``run_kwargs`` are passed straight to
    ``pipeline.run``; only the requested metric keys are extracted. If ``title``
    is omitted it is derived from the config's ``company`` block.
    """
    from src.extract import pipeline

    keys = [s.key for s in segments]
    df = pipeline.run(source, metrics=keys, config=config,
                      verbose=False, **run_kwargs)

    if title is None:
        title = _title_from_config(config)

    wb = build_segments_workbook(
        title, segments, df,
        section=section, consolidated_label=consolidated_label,
        units=units, years=years,
    )
    if out_path:
        wb.save(out_path)
    return wb


def generate_company_segments(
    config,
    source,
    *,
    spec_path=None,
    out_path=None,
    years: tuple[int, int] | None = None,
) -> Workbook:
    """Outline-driven orchestrator: company config + source → Segments workbook.

    Loads the segments outline spec (from ``spec_path``, the config's
    ``segments_file``, or an inline ``segments:`` block), parses it, extracts
    only the mapped metric keys via ``pipeline.run``, and lays the outline out
    with :func:`build_outline_workbook`. This is the single seam the dashboard
    and CLI call — extractor and Excel generator stay independent behind it.
    """
    from src.extract import pipeline
    from src.model.financial_model import load_config

    cfg = load_config(config)
    spec = _load_segments_spec(cfg, config, spec_path)
    if not spec or not spec.get("outline"):
        raise ValueError(f"No segments outline spec found for {config}.")

    rows = parse_outline(spec.get("outline", ""),
                         spec.get("sections", []),
                         spec.get("mapping", {}))
    keys = sorted({rs.key for rs in rows if rs.key})

    df = pipeline.run(source, metrics=keys, config=config, verbose=False)

    title = spec.get("title") or _title_from_config(config)
    wb = build_outline_workbook(
        title, rows, df,
        units=spec.get("units", "P$mn"),
        unit_map=_unit_map(config),
        years=years,
    )
    if out_path:
        wb.save(out_path)
    return wb


def _load_segments_spec(cfg: dict, config_path=None, spec_path=None) -> dict | None:
    """Resolve the segments spec: explicit path → config ``segments_file`` → inline."""
    import yaml

    candidates: list[Path] = []
    if spec_path:
        candidates.append(Path(spec_path))
    sf = cfg.get("segments_file")
    if sf:
        candidates.append(Path(sf))
        if config_path:
            candidates.append(Path(config_path).parent / Path(sf).name)
    for c in candidates:
        if c.exists():
            return yaml.safe_load(c.read_text())

    seg = cfg.get("segments")
    if isinstance(seg, dict) and seg.get("outline"):
        return seg
    return None


def _unit_map(config) -> dict:
    """``{metric_key: unit}`` for FY-sum decisions (sum only currency metrics)."""
    try:
        from src.extract.interface import _load_metric_defs
        defs = _load_metric_defs(config)
    except Exception:
        from src.model.financial_model import METRICS
        defs = METRICS
    return {m.key: m.unit for m in defs}


# ---------------------------------------------------------------------------
# Config helpers + CLI
# ---------------------------------------------------------------------------
def segments_from_config(cfg: dict) -> list[SegmentRow]:
    """Read a ``segments:`` list from a company config dict.

    Each item is ``{label: ..., key: ...}`` (``metric`` accepted as an alias
    for ``key``).
    """
    out: list[SegmentRow] = []
    for item in (cfg.get("segments") or []):
        key = item.get("key") or item.get("metric")
        if key and item.get("label"):
            out.append(SegmentRow(item["label"], key))
    return out


def _title_from_config(config) -> str:
    if not config:
        return "Company"
    from src.model.financial_model import load_config
    co = (load_config(config).get("company") or {})
    name = co.get("name") or "Company"
    ticker = co.get("ticker")
    return f"{ticker}: {name}" if ticker else name


# ---------------------------------------------------------------------------
# Metric-list → Segments helpers (lifted from the former Streamlit dashboard so
# the CLI and tests can reuse them without depending on src.ui).
# ---------------------------------------------------------------------------

def load_metric_defs(config=None):
    """Load metric definitions (incl. sport custom metrics), like the UI did."""
    from src.extract.interface import _load_metric_defs
    return _load_metric_defs(config)


def segments_from_queries(queries: list[str], defs) -> list[SegmentRow]:
    """Map fuzzy metric queries → ordered, de-duplicated ``SegmentRow``s.

    Each query resolves to its canonical key (first match only); the row label
    is the metric's human-readable label. Unmatched queries are dropped.
    """
    from src.extract.interface import MetricResolver

    resolver = MetricResolver(defs)
    resolved = resolver.resolve_many(queries)
    rows: list[SegmentRow] = []
    seen: set[str] = set()
    for r in resolved:
        if r is None or not r.keys:
            continue
        key = r.keys[0]
        if key in seen:
            continue
        seen.add(key)
        mdef = resolver._defs.get(key)
        rows.append(SegmentRow(label=(mdef.label if mdef else key), key=key))
    return rows


def segments_title(name: str, ticker: str = "") -> str:
    """Sheet title in the ``/style`` convention: ``TICKER: Name`` (or ``Name``)."""
    name = (name or "").strip()
    ticker = (ticker or "").strip()
    return f"{ticker}: {name}" if ticker else name


def build_segments_xlsx(title: str, segments: list[SegmentRow], df) -> bytes:
    """Build the Segments workbook and return it as ``.xlsx`` bytes (no temp file)."""
    from io import BytesIO

    wb = build_segments_workbook(title, segments, df)
    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate the standardized Segments Excel sheet.")
    parser.add_argument("--source", required=True,
                        help="URL, directory, or .pdf/.md file (see pipeline.run).")
    parser.add_argument("--config", required=True,
                        help="Company YAML config (must contain a 'segments:' list).")
    parser.add_argument("--out", default="segments.xlsx",
                        help="Output .xlsx path (default: segments.xlsx).")
    parser.add_argument("--title", default=None,
                        help="Sheet title (default: derived from config company).")
    parser.add_argument("--spec", default=None,
                        help="Segments outline spec YAML (overrides config segments_file).")
    args = parser.parse_args()

    from src.model.financial_model import load_config
    cfg = load_config(args.config)

    # Outline mode (preferred): a structured outline spec drives the layout.
    spec = _load_segments_spec(cfg, args.config, args.spec)
    if spec and spec.get("outline"):
        generate_company_segments(
            args.config, args.source, spec_path=args.spec, out_path=args.out,
        )
        n = len({rs.key for rs in parse_outline(
            spec["outline"], spec.get("sections", []), spec.get("mapping", {})) if rs.key})
        print(f"Wrote {args.out} (outline mode, {n} mapped metric(s)).")
        return

    # Flat mode: a simple `segments:` list of {label, key}.
    segments = segments_from_config(cfg)
    if not segments:
        parser.error(f"No segments outline or 'segments:' list found in {args.config}.")

    generate_segments_sheet(
        args.source, segments, config=args.config,
        title=args.title, out_path=args.out,
    )
    print(f"Wrote {args.out} with {len(segments)} segment(s).")


if __name__ == "__main__":
    main()
