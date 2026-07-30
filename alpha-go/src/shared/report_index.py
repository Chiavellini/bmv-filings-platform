"""
report_index.py — Shared report discovery and normalization helpers.

This module keeps directory scanning and URL-download bookkeeping aligned across
the app and the pipeline:
- normalize quarter-style filenames into canonical periods like ``2026-1T``
- group files by period
- pick the best source file per period using report-type precedence
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from pathlib import Path
from typing import Iterable


_PERIOD_PATTERNS = (
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<quarter>[1-4])(?:er|do|to)?[-_ ]*trimestre[-_ ]*(?P<year>20\d{2})(?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<quarter_word>primer|primero|segundo|tercer|tercero|cuarto)[-_ ]*trimestre[-_ ]*(?:y[-_ ]*a[nñ]o[-_ ]*)?(?P<year>20\d{2})(?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<quarter_word_en>first|second|third|fourth)[-_ ]*quarter(?:[-_ ]*and[-_ ]*year)?[-_ ]*(?P<year>20\d{2})(?:[-_ ]*results?)?(?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<year>20\d{2})[-_ ]?(?P<quarter>[1-4])[tq](?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<quarter>[1-4])[tq](?P<year>\d{2,4})(?=bmv|[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[^0-9A-Za-z])20(?P<quarter>[1-4])[tq](?P<year>\d{2,4})(?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<year>20\d{2})[-_ ]?[tq](?P<quarter>[1-4])(?:[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    # Matches Herdez-style "1_T24_...", "2_Q25_...", "4_Q22_..." (quarter_sep_T/Q_year2digit)
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<quarter>[1-4])[-_ ][tq](?P<year>\d{2,4})(?=[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    # Matches Herdez-style "1_TD_2026_..." where TD denotes trimestre.
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<quarter>[1-4])[-_ ][tq]d[-_ ](?P<year>20\d{2}|\d{2})(?=[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    # Matches Chedraui-style "1T-2026", "4T-2025-Dictaminado" (separator between T/Q and year)
    re.compile(
        r"(?:^|[^0-9A-Za-z])(?P<quarter>[1-4])[tq][-_. ](?P<year>20\d{2}|\d{2})(?=[^0-9A-Za-z]|$)",
        re.IGNORECASE,
    ),
    # LOW-PRECEDENCE fallbacks (tried last). Kimberly-Clark names press releases
    # "PR1T26" (quarter glued to a letter prefix) and statements "KCMSIFIC2026-1-ESP"
    # (year-quarter, no T/Q). Allow a glued quarter token and a "YYYY-N" form.
    re.compile(
        r"(?P<quarter>[1-4])[tq](?P<year>\d{2})(?![0-9])",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<year>20\d{2})[-_](?P<quarter>[1-4])(?![0-9])",
        re.IGNORECASE,
    ),
)

_SPANISH_QUARTER_WORDS = {
    "primer": "1",
    "primero": "1",
    "segundo": "2",
    "tercer": "3",
    "tercero": "3",
    "cuarto": "4",
}

_ENGLISH_QUARTER_WORDS = {
    "first": "1",
    "second": "2",
    "third": "3",
    "fourth": "4",
}

_RELEASE_RE = re.compile(r"(?:^|[^a-z])release(?:[^a-z]|$)", re.IGNORECASE)
_EARNINGS_RESULTS_RE = re.compile(r"(?:earnings?|results?)", re.IGNORECASE)
_GRUMA_RELEASE_RE = re.compile(r"(?:^|[^a-z0-9])gruma[-_]?e(?:[^a-z0-9]|$)", re.IGNORECASE)

# Annual/yearly report signal — an integrated/annual report or a SEC 20-F. Only consulted as a
# FALLBACK after the quarter patterns miss (a real quarterly filename never carries these tokens),
# so it cannot re-label any quarterly file; a year-only annual report gets a ``YYYY-FY`` period.
_ANNUAL_SIGNAL_RE = re.compile(
    r"(?:informe[-_ ]*anual|reporte[-_ ]*anual|integrated[-_ ]*(?:annual[-_ ]*)?report"
    r"|annual[-_ ]*report|(?<![a-z])annual(?![a-z])|(?<![a-z])anual(?![a-z])|\b20-?f\b)",
    re.IGNORECASE,
)
_ANNUAL_YEAR_RE = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


@dataclass
class ReportGroup:
    period: str
    pdf_paths: list[Path]
    md_paths: list[Path]
    selected_path: Path | None

    @property
    def has_pdf(self) -> bool:
        return bool(self.pdf_paths)

    @property
    def has_md(self) -> bool:
        return bool(self.md_paths)


def infer_period_label(stem: str) -> str | None:
    """Normalize a report stem into canonical ``YYYY-NT`` form."""
    for pattern in _PERIOD_PATTERNS:
        match = pattern.search(stem)
        if not match:
            continue

        year = match.group("year")
        groups = match.groupdict()
        quarter = groups.get("quarter")
        if quarter is None:
            quarter_word = groups.get("quarter_word")
            quarter = _SPANISH_QUARTER_WORDS.get(quarter_word.lower()) if quarter_word else None
        if quarter is None:
            quarter_word_en = groups.get("quarter_word_en")
            quarter = _ENGLISH_QUARTER_WORDS.get(quarter_word_en.lower()) if quarter_word_en else None
        if quarter is None:
            continue
        year_num = int(year) if len(year) == 4 else 2000 + int(year)
        return f"{year_num:04d}-{int(quarter)}T"
    return _infer_annual_period(stem)


def _infer_annual_period(stem: str) -> str | None:
    """Canonical ``YYYY-FY`` for an annual/integrated report or a 20-F; None if not annual."""
    if not _ANNUAL_SIGNAL_RE.search(stem):
        return None
    year = _ANNUAL_YEAR_RE.search(stem)
    if not year:
        return None
    return f"{int(year.group(1)):04d}-FY"


def period_sort_key(period: str) -> tuple[int, int, str]:
    """Sort canonical period labels chronologically; annual (``YYYY-FY``) sorts after that year's Q4."""
    match = re.fullmatch(r"(?P<year>\d{4})-(?P<quarter>[1-4])T", period)
    if match:
        return (int(match.group("year")), int(match.group("quarter")), period)
    annual = re.fullmatch(r"(?P<year>\d{4})-FY", period)
    if annual:
        return (int(annual.group("year")), 5, period)
    return (9999, 99, period)


def index_report_files(files: Iterable[Path]) -> dict[str, ReportGroup]:
    """Group report files by canonical period and choose one primary file per period."""
    grouped: dict[str, ReportGroup] = {}
    best_key: dict[str, tuple[int, int, str, str]] = {}

    for path in files:
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in {".pdf", ".md"}:
            continue

        period = infer_period_label(path.stem)
        if period is None:
            continue

        group = grouped.setdefault(period, ReportGroup(period, [], [], None))
        if suffix == ".pdf":
            group.pdf_paths.append(path)
        else:
            group.md_paths.append(path)

        key = _candidate_key(path)
        if period not in best_key or key < best_key[period]:
            best_key[period] = key
            group.selected_path = path

    return grouped


def _candidate_key(path: Path) -> tuple[int, int, str, str]:
    stem = path.stem.lower()
    suffix = path.suffix.lower()

    if _RELEASE_RE.search(stem) or _GRUMA_RELEASE_RE.search(stem):
        kind_rank = 0
    elif _EARNINGS_RESULTS_RE.search(stem):
        kind_rank = 1
    else:
        kind_rank = 2

    format_rank = 0 if suffix == ".md" else 1
    return (kind_rank, format_rank, stem, path.name.lower())
