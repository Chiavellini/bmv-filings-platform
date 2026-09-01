"""KOF (Coca-Cola FEMSA) — consolidated blocks, "Adj. EBITDA", total net income.

Three per-company facts this extractor encodes (all of which defeated the
generic tiers at some point):

* **Consolidated vs division blocks.** The release prints the SAME row labels
  ("Total revenues", "Gross profit", "Adj. EBITDA") once in the consolidated
  summary/income statement and once per division (Mexico & Central America
  8,956; South America 6,053 in 2Q26). A bare label match can land on a
  division figure, so rows are only read inside a window latched to a
  CONSOLIDATED heading, and every window stops early at the first
  ``DIVISION`` line.

* **"Adj. EBITDA" labels.** KOF never prints a bare "EBITDA" row, so the
  generic table pattern (anchored ``^EBITDA``) produced nothing at all — the
  metric surfaced as an LLM-ONLY alarm. The label here is ``Adj. EBITDA``
  with footnote parens ("( )", "()()", "(4)(5)"), and the income statement's
  "Adj. EBITDA & CAPEX" COLUMN-HEADER row (whose numbers are the years
  2026/2025) must not match — hence the ``(?!\\s*&)`` guard.

* **Total vs attributable net income.** "Consolidated net income 6,505 …"
  prints ONE line above "Net income attributable to equity holders … 6,211";
  the model tracks the TOTAL including non-controlling interest, and the
  generic search tier latched the attributable line. When the attributable
  and NCI rows are both readable, total must equal their sum (±0.5%) or the
  read is refused — a misparsed row yields an honest MISS, not a confident
  wrong value.

The quarterly summary block ("CONSOLIDATED SECOND QUARTER RESULTS") is
preferred for revenue/gross/operating/EBITDA because its columns are clean
quarter/prior pairs; "CONSOLIDATED FIRST SIX MONTHS RESULTS" and
"CONSOLIDATED FULL YEAR RESULTS" share the row labels but never match the
QUARTER heading regex, so YTD figures cannot leak in. Config
`metric_overrides` in configs/kof.yaml stay as regex_table fallback — this
extractor outranks them via tier_precedence (statement > regex_table).
"""
from __future__ import annotations

import re

from src.extract.custom_registry import STATEMENT_TIER
from src.extract.extract_metrics import MetricRow, parse_number
from src.extract.segment_tables import (NUMERIC_TAIL_RE, collapse,
                                        document_guard, emit, norm, numbers)
from src.model.financial_model import MetricDef

# Consolidated-only section headings. The division pages say "MEXICO & CENTRAL
# AMERICA DIVISION" / "SOUTH AMERICA DIVISION" and never match these.
_SUMMARY_RE = re.compile(
    r"CONSOLIDATED\s+(?:FIRST|SECOND|THIRD|FOURTH)\s+QUARTER\s+RESULTS")
_INCOME_RE = re.compile(r"CONSOLIDATED\s+INCOME\s+STATEMENT")
_BALANCE_RE = re.compile(r"CONSOLIDATED\s+BALANCE\s+SHEET")

# Any division heading ends a consolidated window immediately.
_WINDOW_BREAK_RE = re.compile(r"\bDIVISION\b")

# Optional footnote parens after a label: "( )", "()", "(2)", "(4)(5)".
_FN = r"(?:\s*\(\s*\d?\s*\))*"

# label pattern -> metric key, per window. Longest-first where prefixes collide.
_SUMMARY_ROWS = (
    (re.compile(r"^TOTAL\s+REVENUES" + _FN), "revenue"),
    (re.compile(r"^GROSS\s+PROFIT" + _FN), "gross_profit"),
    (re.compile(r"^OPERATING\s+INCOME" + _FN), "operating_income"),
    # "(?!\s*&)" refuses the "Adj. EBITDA & CAPEX" column-header row, whose
    # only numbers are the years 2026 / 2025.
    (re.compile(r"^ADJ\.?\s*EBITDA(?!\s*&)" + _FN), "ebitda"),
)
_INCOME_ROWS = (
    (re.compile(r"^CONSOLIDATED\s+NET\s+INCOME\b"), "net_income"),
    (re.compile(r"^ADJ\.?\s*EBITDA(?!\s*&)" + _FN), "ebitda"),
    (re.compile(r"^TOTAL\s+REVENUES" + _FN), "revenue"),
)
# Cross-check components for total net income (never emitted themselves).
_ATTRIBUTABLE_RE = re.compile(
    r"^NET\s+INCOME\s+ATTRIBUTABLE\s+TO\s+EQUITY\s+HOLDERS[A-Z\s]*")
_NCI_RE = re.compile(r"^NON-?CONTROLLING\s+INTEREST\b")

_BALANCE_ROWS = (
    (re.compile(r"^TOTAL\s+ASSETS\b"), "total_assets"),
)

_SUMMARY_WINDOW = 14    # heading -> 4 data rows within a few lines
_INCOME_WINDOW = 50     # heading -> net income ~30 lines in (2026-1T layout)
_BALANCE_WINDOW = 30

_XCHECK_TOL = 0.005


def extract_kof(
    text: str,
    metric_defs: list[MetricDef],
    period: str | None = None,
    pdf_path=None,
) -> dict[str, MetricRow]:
    """Consolidated revenue / gross profit / operating income / Adj. EBITDA /
    total net income / total assets from the KOF release."""
    if not text or not document_guard(text, "COCA-COLA FEMSA"):
        return {}

    lines = [norm(collapse(raw)) for raw in text.splitlines()]
    defs = {m.key: m for m in metric_defs}
    found: dict[str, MetricRow] = {}

    _extract_segment_actuals(lines, defs, found)

    for section_re, rows, window in (
        (_SUMMARY_RE, _SUMMARY_ROWS, _SUMMARY_WINDOW),
        (_INCOME_RE, _INCOME_ROWS, _INCOME_WINDOW),
        (_BALANCE_RE, _BALANCE_ROWS, _BALANCE_WINDOW),
    ):
        for start, line in enumerate(lines):
            if not section_re.search(line):
                continue
            _read_window(lines[start + 1:start + 1 + window], rows, defs, found)

    _crosscheck_net_income(lines, found)
    return found


_VOLUME_LABELS = (
    ("MEXICO AND CENTRAL AMERICA", "mxcam"),
    ("SOUTH AMERICA", "southam"),
    ("CAM SOUTH", "cam_south"),
    ("GUATEMALA", "guatemala"),
    ("COLOMBIA", "colombia"),
    ("ARGENTINA", "argentina"),
    ("URUGUAY", "uruguay"),
    ("BRAZIL", "brazil"),
    ("MEXICO", "mexico"),
    ("TOTAL", "consolidated"),
)


def _row_tokens(tail: str) -> list[float]:
    """Numeric table cells, preserving a printed dash as a zero cell."""
    out: list[float] = []
    for token in tail.split():
        if "%" in token:
            continue
        if token == "-":
            out.append(0.0)
            continue
        value = parse_number(token)
        if value is not None:
            out.append(value)
    return out


def _labeled_values(line: str, labels=_VOLUME_LABELS):
    for label, slug in labels:
        m = re.match(rf"^{re.escape(label)}(?:\s+\(\d+\))?\s+", line)
        if m:
            return slug, _row_tokens(line[m.end():])
    return None, []


def _extract_segment_actuals(lines: list[str], defs: dict[str, MetricDef],
                             found: dict[str, MetricRow]) -> None:
    """Country revenue/volume matrix and division P&L from KOF's appendix."""
    volume_rows: dict[str, tuple[list[float], str]] = {}
    revenue_rows: dict[str, tuple[list[float], str]] = {}
    in_appendix = False
    mode = ""
    for line in lines:
        if "QUARTERLY- VOLUME, TRANSACTIONS & REVENUES" in line:
            in_appendix, mode = True, ""
            continue
        if not in_appendix:
            continue
        if "YTD- VOLUME, TRANSACTIONS & REVENUES" in line:
            break
        if line == "VOLUME":
            mode = "volume"
            continue
        if line == "TRANSACTIONS":
            mode = "transactions"
            continue
        if line == "REVENUES":
            mode = "revenues"
            continue
        slug, vals = _labeled_values(line)
        if slug and mode == "volume" and len(vals) >= 10:
            volume_rows[slug] = (vals, line)
        elif slug and mode == "revenues" and len(vals) >= 2:
            revenue_rows[slug] = (vals, line)

    categories = ("sparkling", "water", "bulk", "stills", "total")
    for slug, (vals, line) in volume_rows.items():
        for i, category in enumerate(categories):
            emit(found, defs, f"kof_volume_{slug}_{category}",
                 current=vals[i], prior=vals[i + 5], line=line,
                 tag=STATEMENT_TIER)
    for slug, (vals, line) in revenue_rows.items():
        emit(found, defs, f"kof_revenue_{slug}", current=vals[0], prior=vals[1],
             line=line, tag=STATEMENT_TIER)

    # The model groups Guatemala + CAM South as one Central America block.
    for kind, rows in (("volume", volume_rows), ("revenue", revenue_rows)):
        if not all(k in rows for k in ("guatemala", "cam_south")):
            continue
        left, right = rows["guatemala"][0], rows["cam_south"][0]
        if kind == "volume":
            for i, category in enumerate(categories):
                emit(found, defs, f"kof_volume_cam_{category}",
                     current=left[i] + right[i], prior=left[i + 5] + right[i + 5],
                     line="Guatemala + CAM South", tag=STATEMENT_TIER)
        else:
            emit(found, defs, "kof_revenue_cam", current=left[0] + right[0],
                 prior=left[1] + right[1], line="Guatemala + CAM South revenues",
                 tag=STATEMENT_TIER)

    # These three rows occur in a stable order: consolidated, MX&CAM, South Am.
    avg_rows = [line for line in lines if line.startswith("AVERAGE PRICE PER UNIT CASE")]
    other_rows = [line for line in lines if line.startswith("OTHER OPERATING REVENUES")]
    for slug, rows in (("consolidated", avg_rows[:1]), ("mxcam", avg_rows[1:2]),
                       ("southam", avg_rows[2:3])):
        if rows:
            vals = numbers(rows[0], drop_pct=True)
            if len(vals) >= 2:
                emit(found, defs, f"kof_avg_price_{slug}", current=vals[0],
                     prior=vals[1], line=rows[0], tag=STATEMENT_TIER)
    for slug, rows in (("consolidated", other_rows[:1]), ("mxcam", other_rows[1:2]),
                       ("southam", other_rows[2:3])):
        if rows:
            vals = numbers(rows[0], drop_pct=True)
            if len(vals) >= 2:
                emit(found, defs, f"kof_other_revenue_{slug}", current=vals[0],
                     prior=vals[1], line=rows[0], tag=STATEMENT_TIER)

    flat = " ".join(lines)
    beer = re.search(
        r"BRAZIL INCLUDES BEER REVENUES OF PS\.\s*([\d,.]+) MILLION.*?"
        r"AND PS\.\s*([\d,.]+) MILLION", flat)
    if beer:
        emit(found, defs, "kof_beer_revenue_brazil",
             current=parse_number(beer.group(1)), prior=parse_number(beer.group(2)),
             line=beer.group(0), tag=STATEMENT_TIER)

    _extract_division_profitability(lines, defs, found)


def _extract_division_profitability(lines, defs, found) -> None:
    headings = ((re.compile(r"MEXICO\s*&\s*CENTRAL AMERICA DIVISION RESULTS"), "mxcam"),
                (re.compile(r"SOUTH AMERICA DIVISION RESULTS"), "southam"))
    rows = ((re.compile(r"^GROSS PROFIT\b"), "gp"),
            (re.compile(r"^OPERATING INCOME\b"), "ebit"),
            (re.compile(r"^ADJ\.?\s*EBITDA" + _FN), "ebitda"))
    for i, line in enumerate(lines):
        division = next((slug for pattern, slug in headings if pattern.search(line)), None)
        if division is None:
            continue
        for candidate in lines[i + 1:i + 30]:
            for pattern, metric in rows:
                m = pattern.match(candidate)
                if not m:
                    continue
                key = f"kof_{metric}_{division}"
                if key in found:
                    continue
                tail = candidate[m.end():].strip()
                if not NUMERIC_TAIL_RE.match(tail):
                    continue
                vals = numbers(tail, drop_pct=True)
                if len(vals) >= 2:
                    emit(found, defs, key,
                         current=vals[0], prior=vals[1], line=candidate,
                         tag=STATEMENT_TIER)


def _read_window(window: list[str], rows, defs, found: dict) -> None:
    """First matching value row per key inside one latched window."""
    for line in window:
        if _WINDOW_BREAK_RE.search(line):
            return
        for row_re, key in rows:
            if key in found:
                continue
            m = row_re.match(line)
            if not m:
                continue
            tail = line[m.end():].strip()
            if not NUMERIC_TAIL_RE.match(tail):
                continue
            # Interleaved "% of Rev." / Δ% columns carry a % sign; dropping
            # them leaves [current, prior, (YTD current, YTD prior…)] — the
            # quarter pair always comes first.
            values = numbers(tail, drop_pct=True)
            if len(values) < 2:
                continue
            emit(found, defs, key, current=values[0], prior=values[1],
                 line=line, tag=STATEMENT_TIER)


def _crosscheck_net_income(lines: list[str], found: dict) -> None:
    """Refuse a total net income that contradicts attributable + NCI.

    Soft on absence: the check only fires when BOTH components are readable in
    the income-statement window. Printed values are rounded millions, so the
    components may differ from the total by ±1 (6,211 + 293 = 6,504 vs 6,505).
    """
    row = found.get("net_income")
    if row is None or row.current is None:
        return
    attributable = nci = None
    for start, line in enumerate(lines):
        if not _INCOME_RE.search(line):
            continue
        for wline in lines[start + 1:start + 1 + _INCOME_WINDOW]:
            if _WINDOW_BREAK_RE.search(wline):
                break
            for pattern, current in ((_ATTRIBUTABLE_RE, "attr"), (_NCI_RE, "nci")):
                m = pattern.match(wline)
                if not m:
                    continue
                tail = wline[m.end():].strip()
                if not NUMERIC_TAIL_RE.match(tail):
                    continue
                values = numbers(tail, drop_pct=True)
                if values:
                    if current == "attr" and attributable is None:
                        attributable = values[0]
                    elif current == "nci" and nci is None:
                        nci = values[0]
        if attributable is not None and nci is not None:
            break
    if attributable is None or nci is None:
        return
    total = row.current
    if abs(total - (attributable + nci)) > max(_XCHECK_TOL * abs(total), 1.5):
        found.pop("net_income", None)
