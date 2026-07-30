"""test_table_periods.py — header parsing + period-aware column selection.

Pure, PDF-free unit tests for the selector that lets Tier 2 pick the cell whose
column header matches the document's target quarter instead of blindly taking
the first numeric cell.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from src.extract.table_periods import (
    ColHeader,
    PeriodKey,
    current_column_in_header_line,
    normalize_target_period,
    orient_by_growth,
    parse_header_token,
    select_value_for_period,
)


# ---------------------------------------------------------------------------
# parse_header_token
# ---------------------------------------------------------------------------

def test_parse_quarter_year_tokens():
    assert parse_header_token("1T26") == ColHeader(year=2026, quarter=1)
    assert parse_header_token("1Q26") == ColHeader(year=2026, quarter=1)
    assert parse_header_token("1Q16A") == ColHeader(year=2016, quarter=1)
    assert parse_header_token("1T 26") == ColHeader(year=2026, quarter=1)
    assert parse_header_token("3Q2016") == ColHeader(year=2016, quarter=3)


def test_parse_year_only_and_ytd_and_variation():
    assert parse_header_token("2016A") == ColHeader(year=2016, quarter=None)
    assert parse_header_token("Acum 26").is_ytd is True
    assert parse_header_token("Acumulado").is_ytd is True
    assert parse_header_token("YTD").is_ytd is True
    assert parse_header_token("Var %").is_variation is True
    assert parse_header_token("Δ%").is_variation is True
    assert parse_header_token("%").is_variation is True


def test_variation_token_can_still_carry_period():
    header = parse_header_token("Var 1T16")
    assert header is not None
    assert header.is_variation is True
    assert (header.year, header.quarter) == (2016, 1)


def test_parse_bmv_date_range_headers():
    # BMV income-statement headers carry date ranges, not "1T20" tokens. A single
    # quarter range → that quarter; a multi-quarter range → cumulative (YTD).
    quarter = parse_header_token("Trimestre Año Actual 2020-04-01 - 2020- 06-30")
    assert (quarter.year, quarter.quarter, quarter.is_ytd) == (2020, 2, False)
    cumulative = parse_header_token("Acumulado Año Actual 2020-01-01 - 2020- 06-30")
    assert (cumulative.year, cumulative.quarter, cumulative.is_ytd) == (2020, 2, True)
    prior = parse_header_token("Trimestre Año Anterior 2019-04-01 - 2019-06-30")
    assert (prior.year, prior.quarter, prior.is_ytd) == (2019, 2, False)


def test_select_quarter_over_ytd_with_date_range_headers():
    # Real LACOMER layout: [Acum cur, Acum prior, Trimestre cur, Trimestre prior].
    # Positional would take the YTD column; period selection must take the quarter.
    header = {
        1: "Acumulado Año Actual 2020-01-01 - 2020- 06-30",
        2: "Acumulado Año Anterior 2019-01-01 - 2019- 06-30",
        3: "Trimestre Año Actual 2020-04-01 - 2020- 06-30",
        4: "Trimestre Año Anterior 2019-04-01 - 2019- 06-30",
    }
    cells = [(1, "13,247,889,000"), (2, "10,324,630,000"), (3, "7,063,978,000"), (4, "5,403,009,000")]
    current, prior = select_value_for_period(header, cells, PeriodKey(2020, 2))
    assert current == "7,063,978,000"   # Trimestre (quarter), not the Acumulado column
    assert prior == "5,403,009,000"


def test_parse_header_token_none_for_plain_label():
    assert parse_header_token("Ingresos totales") is None
    assert parse_header_token("") is None
    assert parse_header_token(None) is None


# ---------------------------------------------------------------------------
# normalize_target_period
# ---------------------------------------------------------------------------

def test_normalize_canonical_pipeline_form():
    assert normalize_target_period("2026-1T") == PeriodKey(2026, 1)
    assert normalize_target_period("2016-3Q") == PeriodKey(2016, 3)


def test_normalize_ground_truth_form():
    assert normalize_target_period("1Q16A") == PeriodKey(2016, 1)
    assert normalize_target_period("4Q26") == PeriodKey(2026, 4)
    assert normalize_target_period("1T16") == PeriodKey(2016, 1)


def test_normalize_rejects_junk():
    assert normalize_target_period(None) is None
    assert normalize_target_period("") is None
    assert normalize_target_period("annual report") is None


# ---------------------------------------------------------------------------
# select_value_for_period
# ---------------------------------------------------------------------------

def test_select_picks_target_quarter_not_first_column():
    # Columns: prior-year first, current quarter second.
    header = {0: "1T15", 1: "1T16"}
    cells = [(0, "517,708"), (1, "589,053")]
    current, prior = select_value_for_period(header, cells, PeriodKey(2016, 1))
    assert current == "589,053"   # 1T16, even though it is not the first cell
    assert prior == "517,708"     # 1T15 (same quarter, prior year)


def test_select_skips_variation_and_ytd_columns():
    header = {0: "1T16", 1: "Var 1T16", 2: "Acum 1T16"}
    cells = [(0, "100"), (1, "11%"), (2, "100")]
    current, prior = select_value_for_period(header, cells, PeriodKey(2016, 1))
    assert current == "100"       # the quarter column, not the Var/Acum columns
    assert prior is None


def test_select_returns_none_when_no_column_matches():
    header = {0: "1T15", 1: "1T14"}
    cells = [(0, "10"), (1, "9")]
    assert select_value_for_period(header, cells, PeriodKey(2016, 1)) == (None, None)


def test_select_returns_none_without_headers():
    assert select_value_for_period({}, [(0, "10"), (1, "9")], PeriodKey(2016, 1)) == (None, None)


# ---------------------------------------------------------------------------
# orient_by_growth — value/printed-Δ% tiebreaker (was gmexico._orient / lab)
# ---------------------------------------------------------------------------

def test_orient_by_growth_current_first():
    # columns [current, prior], printed +11.1% ⇒ order unchanged
    assert orient_by_growth(1000.0, 900.0, 11.1) == (1000.0, 900.0)


def test_orient_by_growth_prior_first():
    # columns [prior, current] (2018-style), printed growth of current-over-prior
    # is +11.1% ⇒ the second column is current, so it flips
    assert orient_by_growth(900.0, 1000.0, 11.1) == (1000.0, 900.0)


def test_orient_by_growth_negative_delta():
    # 2020 COVID / parens era: current below prior, printed −11.5%
    cur, prior = orient_by_growth(2408.4, 2719.8, -11.5)
    assert (round(cur, 1), round(prior, 1)) == (2408.4, 2719.8)


def test_orient_by_growth_no_signal_or_zero_is_unchanged():
    assert orient_by_growth(1000.0, 900.0, None) == (1000.0, 900.0)
    assert orient_by_growth(0.0, 900.0, 11.1) == (0.0, 900.0)


# ---------------------------------------------------------------------------
# current_column_in_header_line — header-line → current-column index
# (was gmexico._orient_idx; matches quarter tags OR plain years)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("header,target,expect", [
    ("ESTADO DE RESULTADOS 2T25 2T24", PeriodKey(2025, 2), 0),   # quarter-tag current-first
    ("ESTADO DE RESULTADOS 2T24 2T25", PeriodKey(2025, 2), 1),   # quarter-tag prior-first
    ("(Miles de Dólares) 2025 2026 US$000 %", PeriodKey(2026, 1), 1),  # year-only, current 2nd
    ("(Miles de Dólares) 2025 2024 US$000 %", PeriodKey(2025, 1), 0),  # year-only, current 1st
    # Q4 header: quarter block first, then the FY-year block — quarter branch wins
    ("ESTADO DE RESULTADOS 4T24 4T23 Variación 2024 2023", PeriodKey(2024, 4), 0),
    ("1T15 1T14", PeriodKey(2016, 1), None),                     # no column matches
])
def test_current_column_in_header_line(header, target, expect):
    assert current_column_in_header_line(header, target) == expect
