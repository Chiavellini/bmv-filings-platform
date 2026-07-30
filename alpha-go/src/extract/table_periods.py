"""table_periods.py — header-aware column selection for Tier-2 table extraction.

Tier 2 (``parse_tables``) matches a row by its leftmost label, then has to pick
which numeric cell is the value. The historical rule is positional — first
numeric cell = current, second = prior — which silently breaks when the columns
are ordered like ``[Q_cur, Q_prior, Var%, YTD_cur, YTD_prior]`` or when the 2026
IFRS-16 columns reverse order. This module parses the table's period-header row
and selects the cell whose column matches the document's target quarter, with
prior = the same quarter one year earlier.

Everything degrades to positional: ``select_value_for_period`` returns
``(None, None)`` whenever the header cannot be confidently aligned to the target
period, so callers keep today's behavior and no regression is possible.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class PeriodKey:
    """A quarterly target: 4-digit ``year`` + ``quarter`` in 1..4."""

    year: int
    quarter: int


@dataclass(frozen=True)
class ColHeader:
    """A parsed column header. ``year``/``quarter`` are None when absent."""

    year: int | None
    quarter: int | None
    is_ytd: bool = False
    is_variation: bool = False


def _norm(text: str) -> str:
    """Lower-case, strip accents, collapse whitespace."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", text.lower()).strip()


def _year4(raw: str) -> int:
    """'26' → 2026, '2016' → 2016."""
    return int(raw) if len(raw) == 4 else 2000 + int(raw)


# A quarter+year token: 1T26, 1Q26, 1Q16A, 1Q2016, "1T 26". The (?!\d) keeps the
# year from swallowing a trailing digit and tolerates a trailing letter (16A/16E).
_QTR_YEAR_RE = re.compile(r"([1-4])\s*[tq]\s*'?(\d{4}|\d{2})(?!\d)")
# A standalone 4-digit year: 2016, 2026A.
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})(?!\d)")
# Variation columns: Var, Var%, %Var, Δ/δ, delta, vs, a bare %.
_VARIATION_RE = re.compile(r"var|δ|delta|\bvs\b")
# Year-to-date / cumulative columns: Acum, Acumulado, YTD.
_YTD_RE = re.compile(r"acum|acumulado|ytd|year to date")
# BMV date-range headers, e.g. "Trimestre Año Actual 2020-04-01 - 2020- 06-30"
# (note the OCR may inject spaces around the dashes). Captures (year, month).
_DATE_RE = re.compile(r"(\d{4})\s*-\s*(\d{1,2})\s*-\s*\d{1,2}(?!\d)")


def _quarter_of(month: int) -> int:
    return (month - 1) // 3 + 1


def _date_range_period(norm: str) -> tuple[int, int, bool] | None:
    """Parse a date range into (year, quarter, spans_multiple_quarters).

    The quarter comes from the end date's month; a range whose start and end
    fall in different quarters is cumulative (YTD), e.g. an "Acumulado" column.
    """
    dates = _DATE_RE.findall(norm)
    if not dates:
        return None
    start_year, start_month = int(dates[0][0]), int(dates[0][1])
    end_year, end_month = int(dates[-1][0]), int(dates[-1][1])
    if not (1 <= start_month <= 12 and 1 <= end_month <= 12):
        return None
    spans = (start_year, _quarter_of(start_month)) != (end_year, _quarter_of(end_month))
    return end_year, _quarter_of(end_month), spans


def parse_header_token(token: str | None) -> ColHeader | None:
    """Parse a single column-header string into a :class:`ColHeader`.

    Returns None when the token carries no period signal at all (so the caller
    treats that column as un-typed and skips it during selection).
    """
    if not token:
        return None
    norm = _norm(token)
    if not norm:
        return None

    is_variation = bool(_VARIATION_RE.search(norm)) or norm in {"%", "+/-", "+-"}
    is_ytd = bool(_YTD_RE.search(norm))

    year: int | None = None
    quarter: int | None = None
    m = _QTR_YEAR_RE.search(norm)
    if m:
        quarter = int(m.group(1))
        year = _year4(m.group(2))
    else:
        date_range = _date_range_period(norm)
        if date_range is not None:
            year, quarter, spans = date_range
            if spans:                       # cumulative span (e.g. Acumulado) → YTD
                is_ytd = True
        else:
            ym = _YEAR_RE.search(norm)
            if ym:
                year = int(ym.group(1))

    if quarter is None and year is None and not is_variation and not is_ytd:
        return None
    return ColHeader(year=year, quarter=quarter, is_ytd=is_ytd, is_variation=is_variation)


def normalize_target_period(period: str | None) -> PeriodKey | None:
    """Parse a target period label into a :class:`PeriodKey`.

    Accepts both the canonical pipeline form ``YYYY-NT`` (e.g. ``2026-1T``) and
    the ground-truth/harness form ``NQ{YY}A`` (e.g. ``1Q16A``). Returns None for
    unrecognized shapes.
    """
    if not period:
        return None
    s = period.strip()
    # Canonical "YYYY-NT" / "YYYY-NQ" — year first, then quarter.
    m = re.search(r"(\d{4})\D*([1-4])[TtQq]", s)
    if m:
        return PeriodKey(int(m.group(1)), int(m.group(2)))
    # Harness "NQ{YY}A" / "NQ{YYYY}" / "NT{YY}" — quarter first, then year.
    m = re.search(r"([1-4])[TtQq](\d{2}|\d{4})", s)
    if m:
        return PeriodKey(_year4(m.group(2)), int(m.group(1)))
    return None


def select_value_for_period(
    header_by_col: dict[int, str],
    row_cells: list[tuple[int, str]],
    target: PeriodKey,
    *,
    want_ytd: bool = False,
) -> tuple[str | None, str | None]:
    """Pick (current, prior) cell strings for ``target`` using column headers.

    ``header_by_col`` maps a grid-column index to that column's header string;
    ``row_cells`` is the matched data row as ``(col_index, cell_str)`` pairs
    sharing the same column indexing. ``current`` is the column whose header is
    the target quarter+year; ``prior`` is the same quarter one year earlier.
    Columns flagged variation are always skipped; YTD columns are skipped unless
    ``want_ytd``.

    Returns ``(None, None)`` if no column confidently matches the target quarter,
    signalling the caller to fall back to positional selection.
    """
    prior_key = PeriodKey(target.year - 1, target.quarter)
    current_str: str | None = None
    prior_str: str | None = None
    for col, cell in row_cells:
        header = parse_header_token(header_by_col.get(col))
        if header is None or header.is_variation:
            continue
        if header.is_ytd != want_ytd:
            continue
        if header.quarter is None or header.year is None:
            continue
        if header.year == target.year and header.quarter == target.quarter:
            if current_str is None:
                current_str = cell
        elif header.year == prior_key.year and header.quarter == prior_key.quarter:
            if prior_str is None:
                prior_str = cell
    if current_str is None:
        return (None, None)
    return (current_str, prior_str)
