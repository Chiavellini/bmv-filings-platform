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

Two builders live here. ``build_outline_workbook`` (outline mode) is the
maintained path: auto-inserted Check rows (segment-sum + accounting
identities), comment-only suspect marking (the cell note's red-flag indicator,
no amber fill/font). ``build_segments_workbook`` (flat mode) is LEGACY — kept
for the fixed consolidated+segments layout; it still uses the amber font via
``_value_font`` and gets no auto checks.

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
import ast
import re
from dataclasses import dataclass
from pathlib import Path

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Color, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

# Declarative accounting-identity table (target, op_a, operator, op_b, label) —
# shared with the Python identity audit (validation_report) so the in-sheet
# identity Check rows and the report can never drift.
from src.eval.series_checks import (
    IDENTITIES as _IDENTITY_TABLE,
    IDENTITY_RULES as _IDENTITY_RULES,
)
from src.model.financial_model import (
    default_metric_aggregation,
    metric_calc_names,
    parse_metric_calc,
)

# ---------------------------------------------------------------------------
# Style constants (kept here so the look is easy to tweak in one place)
# ---------------------------------------------------------------------------
_FONT = "Arial"
_BLUE = "FF1F497D"          # hard-input value font color
_GREY = "FF54585A"          # derived-row (YoY / %) font color
_AMBER = "FFB45F06"         # low-confidence / flagged hard-input font color

# Hard inputs get a red-flag comment when extraction confidence is below this
# threshold or the validator flagged the value, so the reader can see at a glance
# which numbers are unverified. Raised to 0.7 (stricter gate) so borderline
# prose/search values are surfaced for manual verification, not just the worst.
# (The legacy flat builder still renders these in amber font via _value_font.)
_LOW_CONF_THRESHOLD = 0.7

FONT_TITLE   = Font(name=_FONT, size=12, bold=True)
FONT_SUB     = Font(name=_FONT, size=10, bold=True)
FONT_SECTION = Font(name=_FONT, size=12, bold=True)
FONT_HEADER  = Font(name=_FONT, size=10, bold=True)
FONT_LABEL   = Font(name=_FONT, size=9, bold=True)
FONT_VALUE   = Font(name=_FONT, size=9, bold=True, color=_BLUE)
FONT_VALUE_LOW = Font(name=_FONT, size=9, bold=True, color=_AMBER)
FONT_DERIVED = Font(name=_FONT, size=9, italic=True, color=_GREY)

# Light-grey banding on data rows (theme background, slightly darkened).
FILL_DATA = PatternFill(patternType="solid",
                        fgColor=Color(theme=0, tint=-0.0499893185216834))
# Fill for the one remaining highlight state (user-requested):
#   red — a metric we EXPECT (keyed data row) that came out blank → fill manually
# Suspect / unverified values carry NO fill: the cell Comment's native red-flag
# indicator is the only marker (open the note for the reason).
FILL_MISSING = PatternFill(patternType="solid", fgColor=Color(rgb="FFF8CBAD"))
# green — a manually-verified override value ([verified] tier) reinforcing the sheet
FILL_VERIFIED = PatternFill(patternType="solid", fgColor=Color(rgb="FFC6EFCE"))

BORDER_BOTTOM = Border(bottom=Side(style="thin"))

NUMFMT_MONEY = '"$"\\ #,##0.0;[Red]\\("$"\\ #,##0.0\\)'
NUMFMT_PCT   = '#,##0.0%;[Red]\\(#,##0.0%\\)'
# Extracted percentage metrics use percentage points (4.4 means 4.4%), unlike
# ratio formulas (0.044 means 4.4%). A literal percent sign avoids Excel's
# automatic ×100 scaling on hard inputs and MetricDef ``* 100`` formulas.
NUMFMT_PCT_POINTS = '#,##0.0\\%;[Red]\\(#,##0.0\\%\\)'
NUMFMT_NUMBER = '#,##0.0;[Red]\\(#,##0.0\\)'
NUMFMT_BPS = '#,##0;[Red]\\(#,##0\\)'

ALIGN_LEFT  = Alignment(horizontal="left")
ALIGN_RIGHT = Alignment(horizontal="right")

# Grid geometry. Value columns start at C and are computed per-year by
# ``_build_columns`` (4 quarter columns + 1 FY column per year, C, D, … AA …),
# so the sheet spans the full extracted history rather than a fixed two years.
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


# Derived-row labels that carry NO extracted data — they render as LIVE Excel
# formulas (or, when their inputs are genuinely absent, get pruned — never left
# as a blank "calculation").
_DERIVED_EXACT = {
    "yoy": "yoy",
    "check": "check",
    "margin": "margin",
    "bps change": "bps_change",
    "fx effect": "blank_note",
    "2-year comp": "two_year",
    "avg store size": "avg_store_size",
    "average store size": "avg_store_size",
}

# Cross-row productivity ratios: normalized label -> (numerator_key, denominator_key,
# number_format). Rendered as ``=IFERROR(num/den,"N/A")``; pruned if either key is
# absent from the extracted frame. (m²/m2 spelling both accepted.)
_RATIO_DERIVED = {
    "effective tax rate": ("tax_expense", "ebt", NUMFMT_PCT),
    "sales per m²":       ("revenue", "total_sales_floor", NUMFMT_MONEY),
    "sales per m2":       ("revenue", "total_sales_floor", NUMFMT_MONEY),
    "sales per store":    ("revenue", "total_units", NUMFMT_MONEY),
    "capex per store":    ("capex", "total_units", NUMFMT_MONEY),
    "capex per m²":       ("capex", "total_sales_floor", NUMFMT_MONEY),
    "capex per m2":       ("capex", "total_sales_floor", NUMFMT_MONEY),
}


def _classify_derived(label: str) -> str | None:
    """Return the derived subtype for a label, or ``None`` if it is not derived."""
    t = re.sub(r"\s+", " ", label.strip().lower())
    if t.startswith((
        "as % of total",
        "% of total",
        "as percent of total",
        "percent of total",
    )):
        return "pct_total"
    if t.startswith(("as % of", "% of")):
        return "pct_consolidated"
    if t.startswith("average price"):
        return "average_price"
    if t in _RATIO_DERIVED:
        return "ratio"
    if t.endswith(" margin"):
        return "margin"
    # "YoY <ratio>" (e.g. "YoY Sales per m²") = YoY of the named ratio row.
    if t in {"yoy", "yoy%", "% yoy"} or t.startswith("yoy "):
        return "yoy"
    if t in {"2 year comp", "2yr comp", "2-yr comp", "two-year comp"}:
        return "two_year"
    if t == "bps" or t.startswith("bps "):
        return "bps_change"
    if t == "check" or t.startswith(("check:", "check ")):
        return "check"
    return _DERIVED_EXACT.get(t)


def _next_nonblank(lines: list[str], i: int) -> str | None:
    for j in range(i + 1, len(lines)):
        if lines[j]:
            return lines[j]
    return None


def _outline_position_key(section: str, label: str, occurrence: int) -> tuple:
    """Internal mapping key for repeated labels in an ordered outline.

    The legacy string mapping remains supported for hand-authored YAML specs.
    Compiler-generated outlines additionally carry this positional key so two
    identically labelled rows in the same (or repeated) section cannot silently
    collapse onto the last canonical metric key.
    """
    return ("__outline_position__", section.strip(), label.strip(), occurrence)


def parse_outline(text: str, sections=(), mapping=None) -> list[RowSpec]:
    """Turn a pasted metric outline into ordered ``RowSpec``s.

    Classification is layered so a label that is a *section* in one place and a
    *data row* in another (e.g. "Mexico") resolves correctly:
      1. blank line                                   → spacer
      2. label is in ``mapping`` (positional, ``Section/Label``, or
         plain ``Label``)                             → data (with key)
      3. derived keyword / "As % of …"                → derived
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
    occurrences: dict[tuple[str, str], int] = {}
    for i, label in enumerate(lines):
        if not label:
            rows.append(RowSpec("spacer", ""))
            continue
        identity = (cur_section, label)
        occurrence = occurrences.get(identity, 0) + 1
        key = (
            mapping.get(_outline_position_key(cur_section, label, occurrence))
            or mapping.get(f"{cur_section}/{label}")
            or mapping.get(f"{prev_data_label}/{label}")
            or mapping.get(label)
        )
        if key:
            rows.append(RowSpec("data", label, key=key))
            occurrences[identity] = occurrence
            prev_data_label = label
            continue
        d = _classify_derived(label)
        if d is not None:
            rows.append(RowSpec("derived", label, derived=d))
            continue
        # A declared section header wins over the "next line is derived → data"
        # heuristic, so a section immediately followed by a derived row (e.g. a
        # productivity section whose first child is "Sales per m²") is not
        # misclassified as a blank data row.
        if label in section_set:
            cur_section = label
            prev_data_label = ""
            rows.append(RowSpec("section", label))
            continue
        nxt = _next_nonblank(lines, i)
        if nxt is not None and _classify_derived(nxt) is not None:
            rows.append(RowSpec("data", label))
            occurrences[identity] = occurrence
            prev_data_label = label
            continue
        rows.append(RowSpec("data", label))
        occurrences[identity] = occurrence
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


def _conf_lookup(df) -> dict:
    """Build ``{(year, quarter): {key: {confidence, flagged, source}}}``.

    Sourced from ``df.attrs['confidence']`` (populated by ``pipeline.run``); empty
    when the frame carries no confidence metadata, in which case every value
    renders with the normal (confident) styling.
    """
    table: dict = {}
    attrs = getattr(df, "attrs", {}) or {}
    conf = attrs.get("confidence") or {}
    for (period, key), info in conf.items():
        m = _PERIOD_RE.match(str(period))
        if not m:
            continue
        yq = (int(m.group(1)), int(m.group(2)))
        table.setdefault(yq, {})[key] = info
    return table


def _suspect_lookup(df) -> dict:
    """Build ``{(year, quarter): {key: reason}}`` from ``df.attrs['suspects']``.

    Populated by ``pipeline.run`` via ``series_checks.compute_cell_suspects``
    (sign / range / magnitude-break suspicion, shared with the verification
    gate); empty when the frame carries no suspects metadata.
    """
    table: dict = {}
    attrs = getattr(df, "attrs", {}) or {}
    for (period, key), reason in (attrs.get("suspects") or {}).items():
        m = _PERIOD_RE.match(str(period))
        if not m:
            continue
        table.setdefault((int(m.group(1)), int(m.group(2))), {})[key] = reason
    return table


def _value_font(conf_lookup: dict, yq, key):
    """Return ``(font, comment_text_or_None)`` for a hard-input cell.

    Amber + an explanatory comment when the value is flagged by the validator or
    its extraction confidence is below the threshold; otherwise the normal blue.
    """
    info = conf_lookup.get(yq, {}).get(key) if key else None
    if not info:
        return FONT_VALUE, None
    score = info.get("confidence", 1.0)
    flagged = info.get("flagged", False)
    if not flagged and score >= _LOW_CONF_THRESHOLD:
        return FONT_VALUE, None
    src = (info.get("source") or "").strip()
    tier = ""
    for t in ("xbrl", "statement", "table", "regex_table", "search", "prose", "llm", "calc"):
        if f"[{t}]" in src:
            tier = t
            break
    reason = "failed a cross-check" if flagged else "low-confidence extraction"
    note = f"Unverified: {reason}"
    if tier:
        note += f" (source: {tier})"
    note += f"; confidence {score:.2f}. Treat with caution."
    return FONT_VALUE_LOW, note


def _cell_annotation(conf_lookup: dict, suspect_lookup: dict, yq, key):
    """Return ``(font, comment_text_or_None)`` for an outline hard-input cell.

    The comment (Excel's native red-flag indicator) is the ONLY suspect marker
    on the outline path — the font stays the normal confident blue and no fill
    is applied. It fires when the validator flagged the cell, extraction
    confidence is below the threshold, or a series check (sign / range /
    magnitude break) named it suspect; the text states which check fired, the
    source tier and confidence, and — when a verification subagent already
    tried and failed — that the cell could not be resolved against the source.
    """
    info = conf_lookup.get(yq, {}).get(key) if key else None
    suspect_reason = suspect_lookup.get(yq, {}).get(key) if key else None
    if not info and not suspect_reason:
        return FONT_VALUE, None
    info = info or {}
    score = info.get("confidence", 1.0)
    flagged = info.get("flagged", False)
    if not suspect_reason and not flagged and score >= _LOW_CONF_THRESHOLD:
        return FONT_VALUE, None
    src = (info.get("source") or "").strip()
    tier = ""
    for t in ("xbrl", "statement", "table", "regex_table", "search", "prose", "llm", "calc"):
        if f"[{t}]" in src:
            tier = t
            break
    if suspect_reason:
        reason = suspect_reason
    elif flagged:
        reason = "failed a cross-check"
    else:
        reason = "low-confidence extraction"
    note = f"Unverified: {reason}"
    if tier:
        note += f" (source: {tier})"
    note += f"; confidence {score:.2f}."
    if info.get("verify_status") == "unresolved":
        note += (" Verification against the source report was attempted and "
                 "could not resolve this cell")
        vnote = (info.get("verify_note") or "").strip()
        note += f": {vnote}." if vnote else "."
    else:
        note += " Treat with caution."
    return FONT_VALUE, note


def _all_years(df, years: tuple[int, int] | None = None) -> list[int]:
    """Return the ascending list of calendar years to lay out as columns.

    ``years`` (kept for backward-compat) is an inclusive ``(start, end)`` range
    override; without it, every year present in ``df['period']`` is rendered so
    the sheet spans the full extracted history (e.g. 2016 → 2026), matching the
    reference templates in ``data/style/``.
    """
    if years:
        lo, hi = years
        return list(range(lo, hi + 1))
    found: set[int] = set()
    if df is not None and "period" in getattr(df, "columns", []):
        for p in df["period"]:
            m = _PERIOD_RE.match(str(p))
            if m:
                found.add(int(m.group(1)))
    return sorted(found)


@dataclass
class ColumnPlan:
    """Computed column geometry for an arbitrary span of years.

    Each year occupies a repeating 5-column block — four quarters then a FY
    total — starting at column C (``A`` is the gutter, ``B`` the label column).
    """
    years: list[int]
    q_col: dict[tuple[int, int], str]   # (year, quarter) -> column letter
    fy_col: dict[int, str]              # year -> FY column letter
    value_cols: list[str]              # every numeric column, left -> right
    prior_col: dict[str, str | None]   # column -> same-quarter column a year back
    previous_period_col: dict[str, str | None]  # quarter -> preceding chronological quarter


def _build_columns(years: list[int]) -> ColumnPlan:
    """Assign quarter + FY columns for every year (C, D, E … past Z to AA …)."""
    q_col: dict[tuple[int, int], str] = {}
    fy_col: dict[int, str] = {}
    value_cols: list[str] = []
    col_idx = 3  # column C
    for y in years:
        for q in (1, 2, 3, 4):
            letter = get_column_letter(col_idx)
            q_col[(y, q)] = letter
            value_cols.append(letter)
            col_idx += 1
        letter = get_column_letter(col_idx)
        fy_col[y] = letter
        value_cols.append(letter)
        col_idx += 1

    yearset = set(years)
    prior_col: dict[str, str | None] = {}
    for y in years:
        for q in (1, 2, 3, 4):
            prior_col[q_col[(y, q)]] = q_col.get((y - 1, q)) if (y - 1) in yearset else None
        prior_col[fy_col[y]] = fy_col.get(y - 1) if (y - 1) in yearset else None
    previous_period_col = {}
    for year in years:
        for quarter in (1, 2, 3, 4):
            previous_period = ((year, quarter - 1) if quarter > 1
                               else (year - 1, 4))
            previous_period_col[q_col[(year, quarter)]] = q_col.get(previous_period)
    return ColumnPlan(years, q_col, fy_col, value_cols, prior_col,
                      previous_period_col)


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


def _yoy_formula(col: str, prior: str, row: int) -> str:
    """Year-over-year: a column vs. the same quarter (or FY) one year earlier."""
    return f'=IFERROR({col}{row}/{prior}{row}-1,"N/A")'


def _bps_formula(col: str, prior: str, row: int) -> str:
    return f'=IFERROR(({col}{row}-{prior}{row})*10000,"N/A")'


def _numfmt_for_unit(unit: str | None) -> str:
    if unit == "pct":
        return NUMFMT_PCT_POINTS
    if unit in {"count", "volume"}:
        return NUMFMT_NUMBER
    return NUMFMT_MONEY


class MetricLayoutMap(dict):
    """Backward-compatible unit map carrying workbook calculation metadata.

    Existing callers can keep treating this as ``{metric_key: unit}``; the
    outline builder also reads the attached aggregation/calculation metadata.
    """

    def __init__(self, units=None, *, aggregations=None, calculations=None,
                 delta_metrics=None, skip_rules=None):
        super().__init__(units or {})
        self.aggregations = dict(aggregations or {})
        self.calculations = dict(calculations or {})
        self.delta_metrics = dict(delta_metrics or {})
        self.skip_rules = set(skip_rules or ())


_EXCEL_CALC_OPERATORS = {
    ast.Add: "+",
    ast.Sub: "-",
    ast.Mult: "*",
    ast.Div: "/",
}
_EXCEL_UNARY_OPERATORS = {ast.UAdd: "+", ast.USub: "-"}

_SOURCE_NAME_ALIASES = {
    "short_term_debt": ("short term debt", "deuda corto", "deuda a corto"),
    "long_term_debt": ("long term debt", "deuda largo", "deuda a largo", "largo plazo"),
    "total_debt": ("total debt", "deuda total", "deuda financiera"),
    "cash": ("cash", "efectivo"),
}


def _compile_excel_calc(expression: str, col: str,
                        row_by_key: dict[str, int]) -> str | None:
    """Compile a safe MetricDef arithmetic expression into a guarded formula."""
    try:
        tree = parse_metric_calc(expression)
        names = metric_calc_names(expression)
        refs = [f"{col}{row_by_key[name]}" for name in names]
    except (KeyError, TypeError, ValueError):
        return None
    if not refs:
        return None

    def visit(node) -> str:
        if isinstance(node, ast.Expression):
            return visit(node.body)
        if isinstance(node, ast.Name):
            return f"{col}{row_by_key[node.id]}"
        if isinstance(node, ast.Constant):
            return str(node.value)
        if isinstance(node, ast.UnaryOp):
            return f"({_EXCEL_UNARY_OPERATORS[type(node.op)]}{visit(node.operand)})"
        if isinstance(node, ast.BinOp):
            symbol = _EXCEL_CALC_OPERATORS[type(node.op)]
            return f"({visit(node.left)}{symbol}{visit(node.right)})"
        raise ValueError(f"Unsupported calculation node: {type(node).__name__}")

    try:
        body = visit(tree)
    except (KeyError, TypeError, ValueError):
        return None
    count_args = ",".join(refs)
    return (f'=IF(COUNT({count_args})={len(refs)},'
            f'IFERROR({body},"N/A"),"N/A")')


def _source_mentions_name(source: str, name: str) -> bool:
    lowered = source.lower()
    spaced = lowered.replace("_", " ").replace("-", " ")
    aliases = (name, name.replace("_", " "), *_SOURCE_NAME_ALIASES.get(name, ()))
    return any(alias.lower() in (lowered if "_" in alias else spaced)
               for alias in aliases)


def _calculated_provenance(info: dict, expression: str) -> bool:
    """True only when source metadata says a populated cell was calculated.

    ``[statement]`` values remain hard inputs unless the source itself explicitly
    names every arithmetic operand and operator (the GMEXICO debt decomposition
    is the live example).  Manual ``[verified]`` overrides always remain inputs.
    """
    source = str((info or {}).get("source") or "").strip()
    lowered = source.lower()
    if lowered.startswith("[verified]"):
        return False
    tier = re.match(r"\[([^]]+)]", lowered)
    if tier and tier.group(1) in {"calc", "calculated", "derived"}:
        return True
    if any(marker in lowered for marker in (" calculated ", " computed ", " derived ")):
        return True
    try:
        tree = parse_metric_calc(expression)
        names = metric_calc_names(expression)
    except (TypeError, ValueError):
        return False
    operators = {
        _EXCEL_CALC_OPERATORS[type(node.op)]
        for node in ast.walk(tree) if isinstance(node, ast.BinOp)
    }
    operators.update(
        _EXCEL_UNARY_OPERATORS[type(node.op)]
        for node in ast.walk(tree) if isinstance(node, ast.UnaryOp)
    )
    operator_aliases = {"-": ("−", "–"), "*": ("×",), "/": ("÷",)}
    source_has_operator = all(
        (symbol in source)
        or any(alias in source for alias in operator_aliases.get(symbol, ()))
        for symbol in operators
    )
    return bool(names and source_has_operator
                and all(_source_mentions_name(source, name) for name in names))


def _metric_aggregation(key: str | None, unit: str | None,
                        aggregation_map: dict[str, str]) -> str:
    configured = aggregation_map.get(key or "")
    if configured in {"sum", "ending", "average", "none"}:
        return configured
    return default_metric_aggregation(key or "", None, unit)


def _fy_aggregation_formula(aggregation: str, plan: ColumnPlan, year: int,
                            row: int) -> str | None:
    q1, q4 = plan.q_col[(year, 1)], plan.q_col[(year, 4)]
    rng = f"{q1}{row}:{q4}{row}"
    if aggregation == "sum":
        return f'=IF(COUNT({rng})=4,SUM({rng}),"N/A")'
    if aggregation == "ending":
        return f'=IF(COUNT({q4}{row})=1,{q4}{row},"N/A")'
    if aggregation == "average":
        return f'=IF(COUNT({rng})=4,AVERAGE({rng}),"N/A")'
    return None


def _metric_family(key: str | None) -> str | None:
    if not key:
        return None
    # Segment families across naming conventions: the generic ``<family>_<seg>``
    # and the compact Herdez prefixes (ns_/gp_/ebit_/equity_/megamex_).
    if key == "revenue" or key.startswith("net_sales_") or key.startswith("ns_"):
        return "revenue"
    if key == "gross_profit" or key.startswith("gross_profit_") or key.startswith("gp_"):
        return "gross_profit"
    if key == "operating_income" or key.startswith("operating_income_") or key.startswith("ebit_"):
        return "operating_income"
    if key == "ebitda" or key.startswith("ebitda_"):
        return "ebitda"
    if key == "equity_associates" or key.startswith("equity_"):
        return "equity"
    if key.startswith("megamex_"):
        return "megamex"
    if key == "volume" or key.startswith("volume_"):
        return "volume"
    return None


def _check_family(key: str | None) -> str | None:
    """Family helper for CHECK rows only: ``_metric_family`` plus the generic
    ``revenue_<seg>`` convention (Walmex/Sport style ``revenue_mexico``).

    Deliberately NOT merged into ``_metric_family``: widening that one would
    silently retarget existing "As % of Consolidated" anchors, which resolve
    their denominator through the same family map.
    """
    fam = _metric_family(key)
    if fam is None and key and key.startswith("revenue_"):
        return "revenue"
    return fam


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
    return {
        "revenue": "revenue",
        "equity": "equity_associates",
        "megamex": "megamex_net_sales",
    }.get(family, family)


def _sales_key_for_suffix(suffix: str) -> str:
    return f"net_sales_{suffix}" if suffix else "revenue"


def _sales_base_key(key: str | None) -> str:
    """The net-sales denominator for a metric's margin (its own segment's sales).

    Consolidated P&L lines → ``revenue``; a Herdez segment line (gp_/ebit_/ebitda_)
    → that segment's net sales (``ns_<seg>``); a MegaMex standalone line →
    ``megamex_net_sales``; otherwise the generic ``net_sales_<suffix>``/revenue.
    """
    if not key:
        return "revenue"
    if key.startswith("megamex_"):
        return "megamex_net_sales"
    for pfx in ("gp_", "ebit_", "ebitda_"):
        if key.startswith(pfx):
            return "ns_" + key[len(pfx):]
    if key.startswith("ns_"):
        return key
    return _sales_key_for_suffix(_metric_suffix(key))


def _volume_key_for_suffix(suffix: str) -> str:
    return f"volume_{suffix}" if suffix else "volume"


# ---------------------------------------------------------------------------
# Row-render planning: which outline rows actually produce content
# ---------------------------------------------------------------------------
# A row "renders" when it writes a value (data) or a formula (derived). Rows that
# would render NOTHING (a keyed metric absent from the frame, a derived calc whose
# inputs are absent, a plain placeholder) are pruned from the workbook AND reported
# by the audit so the outline can be trimmed to match the deliverable 1:1.

def _present_keys(df) -> set[str]:
    keys: set[str] = set()
    for k in getattr(df, "columns", []):
        if k == "period":
            continue
        try:
            if df[k].notna().any():
                keys.add(k)
        except Exception:  # noqa: BLE001
            pass
    return keys


def _formula_capable_data_key(key: str, declared_keys: set[str], unit_map) -> bool:
    """Whether a missing data row can still render as a live Excel formula."""
    calculations = dict(getattr(unit_map, "calculations", {}) or {})
    delta_metrics = dict(getattr(unit_map, "delta_metrics", {}) or {})
    if key in delta_metrics:
        return delta_metrics[key] in declared_keys
    expression = calculations.get(key)
    if not expression:
        return False
    try:
        names = set(metric_calc_names(expression))
        return bool(names) and names <= declared_keys
    except (TypeError, ValueError):
        return False


def _prior_year_periods(periods: set[tuple[int, int]]) -> set[tuple[int, int]]:
    """Periods whose same quarter in the prior year is also evaluable."""
    return {(year, quarter) for year, quarter in periods
            if (year - 1, quarter) in periods}


def _outline_evaluable_periods(rows: list["RowSpec"], df, unit_map) -> list[set[tuple[int, int]]]:
    """Per outline row, quarters where its value/formula can produce a number.

    This mirrors the builder's row references but reasons over cell availability,
    not merely global column presence. It is intentionally used by the strict
    preserve-mode audit: formulas that can only return ``N/A`` are not publishable.
    """
    lookup = _value_lookup(df)
    conf_lookup = _conf_lookup(df)
    declared_keys = {spec.key for spec in rows if spec.kind == "data" and spec.key}
    calculations = dict(getattr(unit_map, "calculations", {}) or {})
    delta_metrics = dict(getattr(unit_map, "delta_metrics", {}) or {})

    def is_numeric(value) -> bool:
        try:
            import math
            return not math.isnan(float(value))
        except (TypeError, ValueError):
            return False

    hard_periods = {
        key: {period for period, values in lookup.items()
              if key in values and is_numeric(values[key])}
        for key in declared_keys
    }
    planned_periods = {
        (year, quarter)
        for year in _all_years(df)
        for quarter in (1, 2, 3, 4)
    }
    cache: dict[str, set[tuple[int, int]]] = {}

    def intersect_metric_periods(names, stack=frozenset()) -> set[tuple[int, int]]:
        names = tuple(names)
        if not names:
            return set()
        result = metric_periods(names[0], stack).copy()
        for name in names[1:]:
            result &= metric_periods(name, stack)
        return result

    def metric_periods(key: str, stack=frozenset()) -> set[tuple[int, int]]:
        if key in cache:
            return cache[key]
        if key in stack or key not in declared_keys:
            return set()
        stack = stack | {key}
        hard = set(hard_periods.get(key, set()))

        delta_base = delta_metrics.get(key)
        if delta_base:
            if delta_base not in declared_keys:
                cache[key] = hard
                return hard
            base_periods = metric_periods(delta_base, stack)
            derived = set()
            for period in planned_periods:
                year, quarter = period
                previous = (year, quarter - 1) if quarter > 1 else (year - 1, 4)
                if period in base_periods and previous in base_periods:
                    derived.add(period)
            # A manual verified delta remains a hard input; all other populated
            # delta cells are replaced by the live period-over-period formula.
            verified = {
                period for period in hard
                if str((conf_lookup.get(period, {}).get(key) or {}).get("source") or "")
                .lower().startswith("[verified]")
            }
            cache[key] = verified | derived
            return cache[key]

        expression = calculations.get(key)
        if not expression:
            cache[key] = hard
            return hard
        try:
            names = metric_calc_names(expression)
        except (TypeError, ValueError):
            cache[key] = hard
            return hard
        if not names or not set(names) <= declared_keys:
            # The builder cannot compile without analyst-sheet operand rows and
            # therefore retains any populated value as a hard input.
            cache[key] = hard
            return hard
        calculated = intersect_metric_periods(names, stack)
        reported = {
            period for period in hard
            if not _calculated_provenance(
                conf_lookup.get(period, {}).get(key) or {}, expression,
            )
        }
        cache[key] = reported | calculated
        return cache[key]

    row_periods: list[set[tuple[int, int]]] = [set() for _ in rows]
    metric_rows = {spec.key: i for i, spec in enumerate(rows)
                   if spec.kind == "data" and spec.key}
    first_data_periods: set[tuple[int, int]] | None = None
    section_anchor_periods: set[tuple[int, int]] | None = None
    last_data_periods: set[tuple[int, int]] | None = None
    last_data_key: str | None = None
    last_yoy_periods: set[tuple[int, int]] | None = None
    last_margin_periods: set[tuple[int, int]] | None = None
    row_by_label: dict[str, set[tuple[int, int]]] = {}
    section_area_periods: set[tuple[int, int]] | None = None
    section_units_periods: set[tuple[int, int]] | None = None
    section_keys: list[str] = []

    def periods_for_key(key: str | None) -> set[tuple[int, int]]:
        return metric_periods(key) if key and key in metric_rows else set()

    for i, spec in enumerate(rows):
        if spec.kind == "spacer":
            continue
        if spec.kind == "section":
            section_anchor_periods = None
            last_margin_periods = None
            section_area_periods = None
            section_units_periods = None
            section_keys = []
            continue
        if spec.kind == "data":
            periods = periods_for_key(spec.key)
            row_periods[i] = periods
            last_data_periods, last_data_key, last_yoy_periods = periods, spec.key, periods
            if first_data_periods is None:
                first_data_periods = periods
            if section_anchor_periods is None:
                section_anchor_periods = periods
            if spec.key:
                section_keys.append(spec.key)
                if "area" in spec.key:
                    section_area_periods = periods
                if "units" in spec.key:
                    section_units_periods = periods
            row_by_label[spec.label.strip().lower()] = periods
            continue

        derived = spec.derived
        periods: set[tuple[int, int]] = set()
        if derived in {"yoy", "two_year"}:
            target = last_yoy_periods or set()
            label = spec.label.strip().lower()
            if derived == "yoy" and label.startswith("yoy ") and label != "yoy":
                target = row_by_label.get(label[4:].strip(), target)
            periods = _prior_year_periods(target)
        elif derived == "ratio":
            numerator, denominator, _fmt = _RATIO_DERIVED[spec.label.strip().lower()]
            periods = periods_for_key(numerator) & periods_for_key(denominator)
            last_yoy_periods = periods
            row_by_label[spec.label.strip().lower()] = periods
        elif derived == "avg_store_size":
            periods = (section_area_periods or set()) & (section_units_periods or set())
        elif derived == "pct_consolidated" and last_data_periods is not None:
            family = _metric_family(last_data_key)
            anchor_key = _consolidated_key_for_family(family)
            anchor = (periods_for_key(anchor_key) if anchor_key in metric_rows
                      else (first_data_periods or set()))
            periods = last_data_periods & anchor
        elif derived == "pct_total" and last_data_periods is not None:
            denominator = set()
            if last_data_key and "units" in last_data_key:
                if "total_units" in metric_rows:
                    denominator = periods_for_key("total_units")
            elif last_data_key and "area" in last_data_key:
                if "total_sales_floor" in metric_rows:
                    denominator = periods_for_key("total_sales_floor")
            if not denominator and not (
                (last_data_key and "units" in last_data_key and "total_units" in metric_rows)
                or (last_data_key and "area" in last_data_key
                    and "total_sales_floor" in metric_rows)
            ):
                denominator = section_anchor_periods or set()
            periods = last_data_periods & denominator
        elif derived == "average_price" and last_data_key:
            suffix = _metric_suffix(last_data_key)
            periods = (periods_for_key(_sales_key_for_suffix(suffix))
                       & periods_for_key(_volume_key_for_suffix(suffix)))
            last_yoy_periods = periods
        elif derived == "margin" and last_data_periods is not None and last_data_key:
            periods = last_data_periods & periods_for_key(_sales_base_key(last_data_key))
            last_margin_periods = periods
        elif derived == "bps_change" and last_margin_periods is not None:
            periods = _prior_year_periods(last_margin_periods)
        elif derived == "check" and (spec.key or last_data_key):
            if spec.key:
                anchor_key = spec.key
                family, family_of = _check_family(anchor_key), _check_family
            else:
                family, family_of = _metric_family(last_data_key), _metric_family
                anchor_key = _consolidated_key_for_family(family)
            segment_keys = [key for key in section_keys
                            if key != anchor_key and family_of(key) == family]
            periods = intersect_metric_periods([anchor_key, *segment_keys])
        elif derived == "identity_check" and spec.key:
            identity = next((item for item in _IDENTITY_TABLE if item[0] == spec.key), None)
            if identity:
                periods = intersect_metric_periods((identity[0], identity[1], identity[3]))
        row_periods[i] = periods

    return row_periods


def _compute_keep(rows: list["RowSpec"], df, *, unit_map=None,
                  preserve_requested_rows: bool = False) -> list[bool]:
    """Per-row: True iff the row will render a value or a formula.

    Mirrors the guards in ``build_outline_workbook`` at the availability level
    (which keys have data), so prune and audit share one source of truth.
    """
    present = _present_keys(df)
    declared_keys = {spec.key for spec in rows if spec.kind == "data" and spec.key}
    available = declared_keys if preserve_requested_rows else present
    keep: list[bool] = [False] * len(rows)
    # rolling context (reset per section where the renderer resets it)
    last_data_present = False
    last_data_key: str | None = None
    section_anchor_present = False
    sec_area = False
    sec_units = False
    margin_kept = False
    yoy_anchor = False
    section_keys: list[str] = []

    for i, spec in enumerate(rows):
        if spec.kind == "spacer":
            keep[i] = True
            continue
        if spec.kind == "section":
            section_anchor_present = False
            sec_area = sec_units = margin_kept = False
            section_keys = []
            keep[i] = True            # provisional; pruned later if no kept child
            continue
        if spec.kind == "data":
            has_formula = bool(spec.key) and _formula_capable_data_key(
                spec.key, declared_keys, unit_map,
            )
            ok = bool(spec.key) and (spec.key in present or has_formula)
            keep[i] = ok
            # The preserve mode mirrors the renderer: every requested keyed row
            # becomes an addressable anchor, even when its hard input is missing.
            if ok or (preserve_requested_rows and spec.key):
                section_keys.append(spec.key)
                last_data_present, last_data_key, yoy_anchor = True, spec.key, True
                section_anchor_present = True
                if "area" in spec.key:
                    sec_area = True
                if "units" in spec.key:
                    sec_units = True
            continue
        # derived
        d, lt = spec.derived, spec.label.strip().lower()
        ok = False
        if d == "yoy":
            ok = yoy_anchor
        elif d == "two_year":
            ok = yoy_anchor
        elif d == "ratio":
            num, den, _ = _RATIO_DERIVED[lt]
            ok = num in available and den in available
            if ok:
                yoy_anchor = True
        elif d == "avg_store_size":
            ok = sec_area and sec_units
            if ok:
                yoy_anchor = True
        elif d == "pct_consolidated":
            ok = last_data_present
        elif d == "pct_total":
            ok = section_anchor_present
        elif d == "average_price":
            sfx = _metric_suffix(last_data_key)
            ok = (_sales_key_for_suffix(sfx) in available
                  and _volume_key_for_suffix(sfx) in available)
            if ok:
                yoy_anchor = True
        elif d == "margin":
            ok = last_data_present and _sales_base_key(last_data_key) in available
            margin_kept = ok
        elif d == "bps_change":
            ok = margin_kept
        elif d == "check":
            if spec.key:
                # auto-inserted (position-independent): anchor travels in spec.key
                fam = _check_family(spec.key)
                anchor = spec.key
                seg = [k for k in section_keys if k != anchor and _check_family(k) == fam]
            else:
                fam = _metric_family(last_data_key)
                anchor = _consolidated_key_for_family(fam)
                seg = [k for k in section_keys if k != anchor and _metric_family(k) == fam]
            ok = bool(anchor) and anchor in section_keys and len(seg) >= 1
        elif d == "identity_check":
            ident = next((it for it in _IDENTITY_TABLE if it[0] == spec.key), None)
            ok = bool(ident) and {ident[0], ident[1], ident[3]} <= available
        elif d == "blank_note":
            # renders an intentional-blank comment on the label — that note IS
            # the content (e.g. "FX Effect" rows), so it survives the no-blank-
            # row rule.
            ok = True
        else:  # plain / unknown → no content
            ok = False
        keep[i] = ok

    # Drop section headers with no kept child before the next section. In
    # preserve mode the analyst explicitly requested the section itself.
    if preserve_requested_rows:
        return keep
    for i, spec in enumerate(rows):
        if spec.kind != "section":
            continue
        has_child = False
        for j in range(i + 1, len(rows)):
            if rows[j].kind == "section":
                break
            if rows[j].kind in ("data", "derived") and keep[j]:
                has_child = True
                break
        if not has_child:
            keep[i] = False
    return keep


def prune_outline(rows: list["RowSpec"], df) -> tuple[list["RowSpec"], list[tuple[str, str]]]:
    """Return (kept_rows, dropped) where dropped is ``[(label, reason)]``.

    Also collapses spacers left orphaned by removed sections/rows.
    """
    keep = _compute_keep(rows, df)
    present = _present_keys(df)
    dropped: list[tuple[str, str]] = []
    for spec, k in zip(rows, keep):
        if k or spec.kind == "spacer":
            continue
        if spec.kind == "data":
            reason = "no value in any period" if spec.key else "unmapped placeholder row"
        elif spec.kind == "section":
            reason = "section has no populated rows"
        else:
            reason = "calculation inputs absent" if spec.derived not in (
                "plain", "blank_note") else "placeholder (no formula)"
        dropped.append((spec.label, reason))
    kept = [spec for spec, k in zip(rows, keep) if k]
    # collapse consecutive / leading / trailing spacers
    cleaned: list[RowSpec] = []
    for spec in kept:
        if spec.kind == "spacer" and (not cleaned or cleaned[-1].kind == "spacer"):
            continue
        cleaned.append(spec)
    while cleaned and cleaned[-1].kind == "spacer":
        cleaned.pop()
    return cleaned, dropped


def audit_outline(rows: list["RowSpec"], df, *, unit_map=None,
                  preserve_requested_rows: bool = False) -> list[dict]:
    """Rows the agent left in the outline that won't render — the Excel-audit signal.

    Returns ``[{label, kind, reason}]``; empty means the outline == the deliverable
    (no blank rows, every calculation is a live formula). Consumed by the gate.
    Pass the same metadata-rich ``unit_map`` and ``preserve_requested_rows=True``
    used by the onboarding builder so absent MetricDef/delta targets are accepted
    when their requested operand rows compile into live formulas.
    """
    keep = _compute_keep(
        rows, df, unit_map=unit_map,
        preserve_requested_rows=preserve_requested_rows,
    )
    evaluable = (_outline_evaluable_periods(rows, df, unit_map)
                 if preserve_requested_rows else [set() for _ in rows])
    declared_keys = {spec.key for spec in rows if spec.kind == "data" and spec.key}
    issues: list[dict] = []
    for i, (spec, k) in enumerate(zip(rows, keep)):
        formula_row = (
            (spec.kind == "data" and bool(spec.key)
             and _formula_capable_data_key(spec.key, declared_keys, unit_map))
            or (spec.kind == "derived" and spec.derived not in {None, "plain", "blank_note"})
        )
        if (preserve_requested_rows and k and formula_row and not evaluable[i]):
            if spec.kind == "data":
                reason = (f"calculated metric '{spec.key}' has zero evaluable periods "
                          "(required operands never overlap)")
            else:
                reason = (f"calculation '{spec.label}' has zero evaluable periods "
                          "(required inputs never overlap)")
            issues.append({"label": spec.label, "kind": spec.kind, "reason": reason})
            continue
        if k or spec.kind == "spacer":
            continue
        if spec.kind == "data" and spec.key:
            reason = f"keyed metric '{spec.key}' is empty in every period"
        elif spec.kind == "data":
            reason = "data row has no metric key (placeholder)"
        elif spec.kind == "section":
            reason = "section header with no populated rows"
        elif spec.derived in ("plain", "blank_note"):
            reason = f"derived row '{spec.label}' is a placeholder with no formula"
        else:
            reason = f"calculation '{spec.label}' has no inputs (renders blank)"
        issues.append({"label": spec.label, "kind": spec.kind, "reason": reason})
    return issues


# ---------------------------------------------------------------------------
# Auto-generated check rows (segment-sum + accounting identities)
# ---------------------------------------------------------------------------
# Live reconciliation formulas are inserted automatically so every deliverable
# carries them — they must not depend on the outline author remembering to
# declare "Check". Candidates are structural: when an analyst asks for every
# operand row, the zero-difference formula is present even when the extracted
# values disagree.  A nonzero result is the signal, not a reason to hide it.


def _derived_tail_end(rows: list["RowSpec"], i: int) -> int:
    """Index just past the contiguous derived rows following ``rows[i]``."""
    j = i + 1
    while j < len(rows) and rows[j].kind == "derived":
        j += 1
    return j


def augment_outline_checks(rows: list["RowSpec"], df, *,
                           skip_rules=None) -> tuple[list["RowSpec"], int]:
    """Insert auto check rows; return (augmented_rows, inserted_count).

    Two kinds, both LIVE formulas rendered by ``build_outline_workbook``:
      - segment-sum "Check" per section/family (``= consolidated − SUM(segments)``),
        skipped when the outline already declares a Check covering that family;
      - accounting-identity "Check: <label>" rows (one per identity per sheet)
        for the trusted identities in ``series_checks.IDENTITIES``.

    Auto rows carry their anchor/target in ``RowSpec.key`` so rendering is
    position-independent. They are inserted whenever all operand rows are in
    the analyst outline; guarded formulas show ``N/A`` for incomplete periods.
    Company-specific validator ``skip_rules`` suppress definitionally-invalid
    checks without making ordinary data disagreements disappear.
    """
    skip_rules = set(skip_rules or ())
    out = list(rows)
    inserted = 0

    # ── segment-sum checks, section by section (insert back-to-front) ──
    bounds: list[tuple[int, int]] = []           # (section_start, end_exclusive)
    start = 0
    for i, spec in enumerate(out):
        if spec.kind == "section":
            if i > start:
                bounds.append((start, i))
            start = i
    bounds.append((start, len(out)))

    for lo, hi in reversed(bounds):
        data_at = [(i, s.key) for i, s in enumerate(out[lo:hi], lo)
                   if s.kind == "data" and s.key]
        keys_here = [k for _, k in data_at]
        # families already covered by an outline-declared Check in this section
        # (its family = the nearest preceding data row's, mirroring the renderer)
        covered: set[str] = set()
        last_key = None
        for i in range(lo, hi):
            s = out[i]
            if s.kind == "data" and s.key:
                last_key = s.key
            elif s.kind == "derived" and s.derived == "check":
                fam = _check_family(s.key) if s.key else _metric_family(last_key)
                if fam:
                    covered.add(fam)
        fams: dict[str, list[str]] = {}
        for k in keys_here:
            fam = _check_family(k)
            if fam:
                fams.setdefault(fam, []).append(k)
        candidates: list[tuple[int, str]] = []       # (insert_anchor_idx, anchor_key)
        for fam, fam_keys in fams.items():
            anchor = _consolidated_key_for_family(fam)
            segs = [k for k in fam_keys if k != anchor]
            family_rule = {"revenue": "revenue_segment_sum"}.get(fam)
            if (fam in covered or not anchor or anchor not in fam_keys or not segs
                    or (family_rule and family_rule in skip_rules)):
                continue
            candidates.append((max(i for i, k in data_at if _check_family(k) == fam),
                               anchor))
        # insert bottom-up so earlier positions in this section stay valid
        for pos, anchor in sorted(candidates, reverse=True):
            out.insert(_derived_tail_end(out, pos),
                       RowSpec("derived", "Check", key=anchor, derived="check"))
            inserted += 1

    # ── identity checks, one per identity per sheet ──
    for ident in reversed(_IDENTITY_TABLE):
        target, op_a, op, op_b, label = ident
        if _IDENTITY_RULES.get(target) in skip_rules:
            continue
        if any(s.kind == "derived" and s.derived == "identity_check" and s.key == target
               for s in out):
            continue
        outline_keys = {s.key for s in out if s.kind == "data" and s.key}
        if not {target, op_a, op_b} <= outline_keys:
            continue
        pos = max(i for i, s in enumerate(out) if s.kind == "data" and s.key == target)
        out.insert(_derived_tail_end(out, pos),
                   RowSpec("derived", f"Check: {label}", key=target,
                           derived="identity_check"))
        inserted += 1

    return out, inserted


# ---------------------------------------------------------------------------
# Sheet builder
# ---------------------------------------------------------------------------
def _hide_unreported_latest_columns(ws: Worksheet, plan: ColumnPlan, lookup: dict) -> None:
    """Hide only the trailing, not-yet-reported columns in the newest year."""
    if not plan.years:
        return
    latest_year = max(plan.years)
    reported_quarters = sorted(
        quarter for year, quarter in lookup if year == latest_year
    )
    if not reported_quarters or reported_quarters[-1] >= 4:
        return
    latest_quarter = reported_quarters[-1]
    for quarter in range(latest_quarter + 1, 5):
        ws.column_dimensions[plan.q_col[(latest_year, quarter)]].hidden = True
    ws.column_dimensions[plan.fy_col[latest_year]].hidden = True


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
    plan = _build_columns(_all_years(df, years))
    lookup = _value_lookup(df)
    conf_lookup = _conf_lookup(df)

    wb = Workbook()
    _set_default_font(wb)
    ws = wb.active
    ws.title = "Segments"
    ws.sheet_view.showGridLines = False

    # Column widths.
    ws.column_dimensions["A"].width = 1.5
    ws.column_dimensions["B"].width = 43.1640625
    for col in plan.value_cols:
        ws.column_dimensions[col].width = 11.6640625

    # Future columns stay in the formula geometry for later refreshes but are
    # not presented as missing analyst data.
    _hide_unreported_latest_columns(ws, plan, lookup)

    # Title block.
    _put(ws, "A2", title, font=FONT_TITLE, align=ALIGN_LEFT)
    _put(ws, "A3", "Segment data", font=FONT_SUB, align=ALIGN_LEFT)

    # Period header row.
    _write_header(ws, units, plan)

    # Section header.
    _put(ws, f"B{_SECTION_ROW}", section,
         font=FONT_SECTION, align=ALIGN_LEFT, border=BORDER_BOTTOM)

    # Segment data row numbers (deterministic, two-pass-friendly).
    seg_rows = [_FIRST_SEG_ROW + i * _BLOCK for i in range(len(segments))]

    # --- Consolidated total (sum of segment rows) + its YoY ----------------
    _put(ws, f"B{_CONS_ROW}", consolidated_label,
         font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
    for y in plan.years:
        for q in (1, 2, 3, 4):
            col = plan.q_col[(y, q)]
            formula = "=" + "+".join(f"{col}{r}" for r in seg_rows) if seg_rows else None
            _put(ws, f"{col}{_CONS_ROW}", formula,
                 font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
        fy = plan.fy_col[y]
        _put(ws, f"{fy}{_CONS_ROW}",
             f"=SUM({plan.q_col[(y, 1)]}{_CONS_ROW}:{plan.q_col[(y, 4)]}{_CONS_ROW})",
             font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)

    yoy_row = _CONS_ROW + 1
    _put(ws, f"B{yoy_row}", "YoY", font=FONT_DERIVED, align=ALIGN_LEFT)
    for col in plan.value_cols:
        prior = plan.prior_col.get(col)
        if prior:
            _put(ws, f"{col}{yoy_row}", _yoy_formula(col, prior, _CONS_ROW),
                 font=FONT_DERIVED, numfmt=NUMFMT_PCT)

    # --- One block per segment --------------------------------------------
    for seg, data_row in zip(segments, seg_rows):
        # Data row — fill hard inputs from the extractor.
        _put(ws, f"B{data_row}", seg.label,
             font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
        for y in plan.years:
            for q in (1, 2, 3, 4):
                col = plan.q_col[(y, q)]
                v = lookup.get((y, q), {}).get(seg.key)
                font, note = (_value_font(conf_lookup, (y, q), seg.key)
                              if v is not None else (FONT_VALUE, None))
                _put(ws, f"{col}{data_row}", v,
                     font=font, numfmt=NUMFMT_MONEY, fill=FILL_DATA)
                if note:
                    ws[f"{col}{data_row}"].comment = Comment(note, "validator")
            fy = plan.fy_col[y]
            _put(ws, f"{fy}{data_row}",
                 f"=SUM({plan.q_col[(y, 1)]}{data_row}:{plan.q_col[(y, 4)]}{data_row})",
                 font=FONT_VALUE, numfmt=NUMFMT_MONEY, fill=FILL_DATA)

        # YoY row.
        r_yoy = data_row + 1
        _put(ws, f"B{r_yoy}", "YoY", font=FONT_DERIVED, align=ALIGN_LEFT)
        for col in plan.value_cols:
            prior = plan.prior_col.get(col)
            if prior:
                _put(ws, f"{col}{r_yoy}", _yoy_formula(col, prior, data_row),
                     font=FONT_DERIVED, numfmt=NUMFMT_PCT)

        # As % of Consolidated row.
        r_pct = data_row + 2
        _put(ws, f"B{r_pct}", "As % of Consolidated",
             font=FONT_DERIVED, align=ALIGN_LEFT)
        for col in plan.value_cols:
            _put(ws, f"{col}{r_pct}", f"=+{col}{data_row}/{col}${_CONS_ROW}",
                 font=FONT_DERIVED, numfmt=NUMFMT_PCT)

    _arialize(ws)
    ws.freeze_panes = "C8"
    return wb


def _write_header(ws, units: str, plan: ColumnPlan) -> None:
    """Write the period header row (row 5): one 4Q+FY block per year."""
    _put(ws, f"B{_HEADER_ROW}", f"Segments (in {units})",
         font=FONT_HEADER, align=ALIGN_LEFT, border=BORDER_BOTTOM)
    for y in plan.years:
        yy = y % 100
        for q in (1, 2, 3, 4):
            _put(ws, f"{plan.q_col[(y, q)]}{_HEADER_ROW}", f"{q}Q{yy:02d}A",
                 font=FONT_HEADER, border=BORDER_BOTTOM)
        _put(ws, f"{plan.fy_col[y]}{_HEADER_ROW}", f"FY{yy:02d}A",
             font=FONT_HEADER, border=BORDER_BOTTOM)


def _set_default_font(wb: Workbook) -> None:
    """Make Arial the workbook's base font so blank/spacer cells aren't Calibri."""
    wb._named_styles["Normal"].font = Font(name=_FONT, size=10)


def _arialize(ws: Worksheet) -> None:
    """Stamp Arial on every cell in the used range, like the reference templates.

    Setting the Normal style alone leaves already-materialized blank/spacer cells
    at Excel's Calibri default; the reference workbooks in ``data/style/`` carry
    Arial on those cells too, so we restamp any non-Arial cell here (preserving
    size / bold / italic / color).
    """
    for row in ws.iter_rows():
        for cell in row:
            f = cell.font
            if f is None or f.name != _FONT:
                cell.font = Font(
                    name=_FONT,
                    size=f.size if f and f.size else 10,
                    bold=f.bold if f else False,
                    italic=f.italic if f else False,
                    color=f.color if f else None,
                )


def build_outline_workbook(
    title: str,
    rows: list[RowSpec],
    df,
    *,
    units: str = "P$mn",
    unit_map: dict | None = None,
    years: tuple[int, int] | None = None,
    subtitle: str = "Segment data",
    auto_checks: bool = True,
    preserve_requested_rows: bool = False,
) -> Workbook:
    """Lay out an arbitrary outline verbatim into the standardized Segments sheet.

    ``rows`` come from :func:`parse_outline`. Data rows are filled from ``df``
    (blank when the key is absent / not extracted); ``YoY`` and ``As % of …``
    rows become Excel formulas; other derived rows (``Margin``, ``bps change``,
    ``Check`` …) are label-only placeholders. FY behavior comes from each
    metric's explicit ``sum``/``ending``/``average``/``none`` aggregation.

    ``auto_checks`` (default on) inserts live reconciliation Check rows the
    outline didn't declare — segment-sum per section + trusted accounting
    identities — via :func:`augment_outline_checks`. Set False (or
    ``auto_checks: false`` in the company config) for companies whose disclosed
    basis makes a check structurally nonzero.

    ``preserve_requested_rows`` keeps every analyst-declared row and section in
    sequence. Missing keyed cells remain visible red blanks, while calculation-
    capable rows still compile formulas against the requested operand rows.
    The default ``False`` preserves the legacy no-empty-row publishing behavior.
    """
    plan = _build_columns(_all_years(df, years))
    lookup = _value_lookup(df)
    conf_lookup = _conf_lookup(df)
    suspect_lookup = _suspect_lookup(df)
    if unit_map is None:
        unit_map = {}
    aggregation_map = dict(getattr(unit_map, "aggregations", {}) or {})
    calculation_map = dict(getattr(unit_map, "calculations", {}) or {})
    delta_metrics = dict(getattr(unit_map, "delta_metrics", {}) or {})
    skip_rules = set(getattr(unit_map, "skip_rules", set()) or set())
    if auto_checks:
        rows, _n_auto = augment_outline_checks(rows, df, skip_rules=skip_rules)
    # Prune rows that would render nothing (empty keyed metrics, calcs with absent
    # inputs, placeholders) + orphaned section headers — never ship a blank row.
    if not preserve_requested_rows:
        rows, _pruned = prune_outline(rows, df)
    # Pre-assign final row numbers so cross-row formulas can reference a key defined
    # LATER in the outline (e.g. per-format "As % of Total" → the Totals row below).
    # The render loop increments r by 1 per spec, so this mirrors it exactly.
    prebuilt_rows: dict[str, int] = {}
    _rr = _HEADER_ROW + 1
    for _spec in rows:
        if _spec.kind == "data" and _spec.key:
            prebuilt_rows[_spec.key] = _rr
        _rr += 1

    wb = Workbook()
    _set_default_font(wb)
    ws = wb.active
    ws.title = "Segments"
    ws.sheet_view.showGridLines = False
    ws.column_dimensions["A"].width = 1.5
    ws.column_dimensions["B"].width = 43.1640625
    for col in plan.value_cols:
        ws.column_dimensions[col].width = 11.6640625
    _hide_unreported_latest_columns(ws, plan, lookup)

    _put(ws, "A2", title, font=FONT_TITLE, align=ALIGN_LEFT)
    _put(ws, "A3", subtitle, font=FONT_SUB, align=ALIGN_LEFT)
    # Legend for the manual-review marks (row 4, above the header). Suspect
    # cells carry no fill — the comment's red-flag indicator is the marker.
    _put(ws, "A4", "Key:", font=FONT_LABEL, align=ALIGN_LEFT)
    flag = ws["B4"]
    flag.value = "  comment marker = suspect / unverified — open the note"
    flag.font = FONT_DERIVED
    red_col = plan.value_cols[0] if plan.value_cols else "C"
    red = ws[f"{red_col}4"]
    red.value = "  missing — verify"
    red.fill, red.font = FILL_MISSING, FONT_DERIVED
    green_col = plan.value_cols[1] if len(plan.value_cols) > 1 else red_col
    green = ws[f"{green_col}4"]
    green.value = "  verified"
    green.fill, green.font = FILL_VERIFIED, FONT_DERIVED
    _write_header(ws, units, plan)

    r = _HEADER_ROW + 1
    first_data_row: int | None = None    # fallback global consolidated anchor
    section_anchor: int | None = None    # first data row in current section (% of Total)
    last_data_row: int | None = None     # nearest hard-input data row above
    last_data_key: str | None = None
    last_yoy_anchor: int | None = None
    last_margin_row: int | None = None
    data_row_by_key: dict[str, int] = dict(prebuilt_rows)   # all keys, incl. forward refs
    row_by_label: dict[str, int] = {}        # normalized label -> row (for "YoY <ratio>")
    section_area_row: int | None = None      # latest area_* row in section (Avg Store Size)
    section_units_row: int | None = None     # latest units_* row in section
    section_keys: list[str] = []             # data keys in the current section (Check scope)

    for spec in rows:
        if spec.kind == "spacer":
            r += 1
            continue

        if spec.kind == "section":
            _put(ws, f"B{r}", spec.label,
                 font=FONT_SECTION, align=ALIGN_LEFT, border=BORDER_BOTTOM)
            section_anchor = None
            last_margin_row = None
            section_area_row = None
            section_units_row = None
            section_keys = []
            r += 1
            continue

        if spec.kind == "data":
            unit = unit_map.get(spec.key, "currency") if spec.key else None
            aggregation = _metric_aggregation(spec.key, unit, aggregation_map)
            calc_expression = calculation_map.get(spec.key or "")
            delta_base = delta_metrics.get(spec.key or "")
            numfmt = _numfmt_for_unit(unit)
            _put(ws, f"B{r}", spec.label,
                 font=FONT_LABEL, align=ALIGN_LEFT, fill=FILL_DATA)
            for y in plan.years:
                for q in (1, 2, 3, 4):
                    col = plan.q_col[(y, q)]
                    v = lookup.get((y, q), {}).get(spec.key) if spec.key else None
                    info = conf_lookup.get((y, q), {}).get(spec.key) or {}
                    source = str(info.get("source") or "")
                    formula = None
                    if delta_base and not source.lower().startswith("[verified]"):
                        base_row = data_row_by_key.get(delta_base)
                        previous = plan.previous_period_col.get(col)
                        if base_row and previous:
                            formula = (f'=IF(COUNT({col}{base_row},{previous}{base_row})=2,'
                                       f'{col}{base_row}-{previous}{base_row},"N/A")')
                    elif calc_expression and (
                        v is None or _calculated_provenance(info, calc_expression)
                    ):
                        formula = _compile_excel_calc(
                            calc_expression, col, data_row_by_key,
                        )

                    if formula is not None:
                        font, note, cell_fill = FONT_DERIVED, None, FILL_DATA
                        cell_value = formula
                    elif v is not None:
                        font, note = _cell_annotation(conf_lookup, suspect_lookup,
                                                      (y, q), spec.key)
                        if "[verified]" in (info.get("source") or ""):
                            # green — a manually-verified override reinforcing the sheet
                            cell_fill = FILL_VERIFIED
                        else:
                            # suspect cells keep the normal banding: the comment's
                            # red-flag indicator is the only visual marker
                            cell_fill = FILL_DATA
                        cell_value = v
                    else:
                        font, note = FONT_VALUE, None
                        # red-highlight a keyed cell that came out blank — a metric
                        # we expected but didn't extract → needs manual verification.
                        cell_fill = FILL_MISSING if spec.key else FILL_DATA
                        cell_value = None
                    _put(ws, f"{col}{r}", cell_value, font=font,
                         numfmt=numfmt, fill=cell_fill)
                    if note:
                        ws[f"{col}{r}"].comment = Comment(note, "verification",
                                                          height=110, width=320)
                fy = plan.fy_col[y]
                fy_formula = _fy_aggregation_formula(aggregation, plan, y, r)
                if fy_formula is None and calc_expression:
                    fy_formula = _compile_excel_calc(
                        calc_expression, fy, data_row_by_key,
                    )
                _put(ws, f"{fy}{r}", fy_formula,
                     font=FONT_DERIVED if fy_formula else FONT_VALUE,
                     numfmt=numfmt, fill=FILL_DATA)
            last_data_row = r
            last_data_key = spec.key
            last_yoy_anchor = r
            if first_data_row is None:
                first_data_row = r
            if section_anchor is None:
                section_anchor = r
            if spec.key:
                data_row_by_key[spec.key] = r
                section_keys.append(spec.key)
                if "area" in spec.key:        # area_hiper, sodimac_area, …
                    section_area_row = r
                if "units" in spec.key:       # units_hiper, sodimac_units, total_units
                    section_units_row = r
            row_by_label[spec.label.strip().lower()] = r
            r += 1
            continue

        if spec.kind == "derived":
            _put(ws, f"B{r}", spec.label, font=FONT_DERIVED, align=ALIGN_LEFT)
            if spec.derived == "yoy":
                # "YoY <ratio>" references the named ratio/data row; bare "YoY" uses
                # the most recent data/ratio anchor.
                lt = spec.label.strip().lower()
                target = last_yoy_anchor
                if lt.startswith("yoy ") and lt != "yoy":
                    target = row_by_label.get(lt[4:].strip(), target)
                if target:
                    for col in plan.value_cols:
                        prior = plan.prior_col.get(col)
                        if prior:
                            _put(ws, f"{col}{r}", _yoy_formula(col, prior, target),
                                 font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "two_year" and last_yoy_anchor:
                # 2-year stacked comp of a rate (e.g. SSS): (1+this)*(1+prior_1yr)-1.
                a = last_yoy_anchor
                for col in plan.value_cols:
                    prior = plan.prior_col.get(col)
                    if prior:
                        _put(ws, f"{col}{r}",
                             f'=IFERROR((1+{col}{a})*(1+{prior}{a})-1,"N/A")',
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "ratio":
                num_key, den_key, fmt = _RATIO_DERIVED[spec.label.strip().lower()]
                num_row = data_row_by_key.get(num_key)
                den_row = data_row_by_key.get(den_key)
                if num_row and den_row:
                    for col in plan.value_cols:
                        _put(ws, f"{col}{r}",
                             f'=IFERROR({col}{num_row}/{col}{den_row},"N/A")',
                             font=FONT_DERIVED, numfmt=fmt)
                    last_yoy_anchor = r        # so a following "YoY <ratio>" references it
                    row_by_label[spec.label.strip().lower()] = r
            elif spec.derived == "avg_store_size" and section_area_row and section_units_row:
                for col in plan.value_cols:
                    _put(ws, f"{col}{r}",
                         f'=IFERROR({col}{section_area_row}/{col}{section_units_row},"N/A")',
                         font=FONT_DERIVED, numfmt=NUMFMT_NUMBER)
            elif spec.derived == "pct_consolidated" and last_data_row:
                family = _metric_family(last_data_key)
                anchor_key = _consolidated_key_for_family(family)
                anchor_row = data_row_by_key.get(anchor_key or "") or first_data_row
                if anchor_row:
                    for col in plan.value_cols:
                        _put(ws, f"{col}{r}", f"=+{col}{last_data_row}/{col}${anchor_row}",
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "pct_total" and last_data_row:
                # % of the GLOBAL total for this metric type (a per-format units row
                # → total_units; an area row → total_sales_floor), not the section's
                # own first row. Fall back to the section anchor when no total exists.
                denom_row = None
                if last_data_key and "units" in last_data_key:
                    denom_row = data_row_by_key.get("total_units")
                elif last_data_key and "area" in last_data_key:
                    denom_row = data_row_by_key.get("total_sales_floor")
                denom_row = denom_row or section_anchor
                if denom_row:
                    for col in plan.value_cols:
                        _put(ws, f"{col}{r}", f"=+{col}{last_data_row}/{col}${denom_row}",
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
            elif spec.derived == "average_price" and last_data_key:
                suffix = _metric_suffix(last_data_key)
                sales_row = data_row_by_key.get(_sales_key_for_suffix(suffix))
                volume_row = data_row_by_key.get(_volume_key_for_suffix(suffix))
                if sales_row and volume_row:
                    for col in plan.value_cols:
                        _put(ws, f"{col}{r}", f'=IFERROR({col}{sales_row}/{col}{volume_row},"N/A")',
                             font=FONT_DERIVED, numfmt=NUMFMT_MONEY)
                    last_yoy_anchor = r
            elif spec.derived == "margin" and last_data_row and last_data_key:
                last_margin_row = None
                sales_row = data_row_by_key.get(_sales_base_key(last_data_key))
                if sales_row:
                    for col in plan.value_cols:
                        _put(ws, f"{col}{r}", f'=IFERROR({col}{last_data_row}/{col}{sales_row},"N/A")',
                             font=FONT_DERIVED, numfmt=NUMFMT_PCT)
                    last_margin_row = r
            elif spec.derived == "bps_change" and last_margin_row:
                for col in plan.value_cols:
                    prior = plan.prior_col.get(col)
                    if prior:
                        _put(ws, f"{col}{r}", _bps_formula(col, prior, last_margin_row),
                             font=FONT_DERIVED, numfmt=NUMFMT_BPS)
            elif spec.derived == "check" and (spec.key or last_data_key):
                # Section-scoped reconciliation: consolidated − Σ(same-family segments
                # IN THIS SECTION). Scoping avoids double-counting when the same total
                # has two segmentations elsewhere (e.g. Herdez Domestic vs Conservas+Impulso).
                # Auto-inserted rows carry the anchor in spec.key (position-independent,
                # wider _check_family); outline-declared rows keep the position-based
                # resolution so existing outlines render identically.
                if spec.key:
                    anchor_key = spec.key
                    family, fam_of = _check_family(anchor_key), _check_family
                else:
                    family, fam_of = _metric_family(last_data_key), _metric_family
                    anchor_key = _consolidated_key_for_family(family)
                anchor_row = data_row_by_key.get(anchor_key or "") if anchor_key in section_keys else None
                segment_rows = [
                    data_row_by_key[key] for key in section_keys
                    if key != anchor_key and fam_of(key) == family
                ]
                if anchor_row and segment_rows:
                    for col in plan.value_cols:
                        refs = [f"{col}{anchor_row}",
                                *(f"{col}{row}" for row in segment_rows)]
                        formula = (f'=IF(COUNT({",".join(refs)})={len(refs)},'
                                   f'{col}{anchor_row}-SUM('
                                   + ",".join(f"{col}{row}" for row in segment_rows)
                                   + '),"N/A")')
                        _put(ws, f"{col}{r}", formula, font=FONT_DERIVED,
                             numfmt=_numfmt_for_unit(unit_map.get(anchor_key or "", "currency")))
            elif spec.derived == "identity_check" and spec.key:
                # Live accounting-identity reconciliation, e.g. "=gross_profit −
                # (revenue − cogs)"; a nonzero exposes an extraction error on one
                # of the three rows for that period.
                ident = next((it for it in _IDENTITY_TABLE if it[0] == spec.key), None)
                if ident:
                    _t, op_a, op, op_b, _lbl = ident
                    t_row = data_row_by_key.get(spec.key)
                    a_row = data_row_by_key.get(op_a)
                    b_row = data_row_by_key.get(op_b)
                    if t_row and a_row and b_row:
                        for col in plan.value_cols:
                            _put(ws, f"{col}{r}",
                                 f'=IF(COUNT({col}{t_row},{col}{a_row},{col}{b_row})=3,'
                                 f'{col}{t_row}-({col}{a_row}{op}{col}{b_row}),"N/A")',
                                 font=FONT_DERIVED,
                                 numfmt=_numfmt_for_unit(unit_map.get(spec.key, "currency")))
            elif spec.derived == "blank_note":
                ws[f"B{r}"].comment = Comment(
                    "Metric unavailable in the source table; left blank intentionally.",
                    "Codex",
                )
            r += 1
            continue

    _arialize(ws)
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
        units=spec.get("units", _default_units(cfg)),
        unit_map=_unit_map(config),
        years=years,
        auto_checks=bool(cfg.get("auto_checks", True)),
    )
    if out_path:
        wb.save(out_path)
    return wb


def _default_units(cfg: dict) -> str:
    """Workbook units header from ``company.currency``; explicit spec wins."""
    currency = str(((cfg or {}).get("company") or {}).get("currency") or "").upper()
    return "US$mn" if currency == "USD" else "P$mn"


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


def _unit_map(config) -> MetricLayoutMap:
    """Metric units plus calculation metadata for the outline builder.

    The return value remains dict-compatible so existing orchestration needs no
    signature change. ``delta_metrics`` are flows even when their unit is a
    count, so their FY aggregation is explicitly overridden to ``sum``.
    """
    cfg = {}
    try:
        if isinstance(config, dict):
            cfg = config
        elif config:
            from src.model.financial_model import load_config
            cfg = load_config(config)
    except Exception:  # noqa: BLE001 - unit fallback remains usable
        cfg = {}
    try:
        if isinstance(config, dict):
            from src.model.financial_model import METRICS, apply_config
            defs = apply_config(METRICS, cfg)
        else:
            from src.extract.interface import _load_metric_defs
            defs = _load_metric_defs(config)
    except Exception:
        from src.model.financial_model import METRICS
        defs = METRICS
    units = {m.key: m.unit for m in defs}
    aggregations = {
        m.key: (m.aggregation or default_metric_aggregation(m.key, m.section, m.unit))
        for m in defs
    }
    calculations = {m.key: m.calc for m in defs if m.calc}
    delta_metrics = dict((cfg.get("delta_metrics") or {}).items())
    for target in delta_metrics:
        aggregations[target] = "sum"
    skip_rules = set(((cfg.get("validator") or {}).get("skip_rules") or ()))
    return MetricLayoutMap(
        units,
        aggregations=aggregations,
        calculations=calculations,
        delta_metrics=delta_metrics,
        skip_rules=skip_rules,
    )


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
