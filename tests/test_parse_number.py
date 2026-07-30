"""
test_parse_number.py — Unit tests for parse_number(), covering all edge cases
encountered in real SPORT quarterly reports.
"""

import pytest
from src.extract.extract_metrics import parse_number


class TestParseNumberBasic:
    """Standard number formats."""

    @pytest.mark.parametrize("s,expected", [
        ("1,234.5",    1234.5),
        ("314,925",    314925.0),
        ("589,053",    589053.0),
        ("1,234,567",  1234567.0),
        ("45,179",     45179.0),
        ("877,678",    877678.0),
        ("0",          0.0),
        ("100",        100.0),
        ("9.3",        9.3),
        ("36.3",       36.3),
        ("5.6",        5.6),
    ])
    def test_positive_numbers(self, s, expected):
        assert parse_number(s) == pytest.approx(expected)

    @pytest.mark.parametrize("s,expected", [
        ("-94,052",   -94052.0),
        ("-66.0",     -66.0),
        ("-3.2",      -3.2),
        ("-121.7",    -121.7),
    ])
    def test_negative_with_sign(self, s, expected):
        assert parse_number(s) == pytest.approx(expected)

    @pytest.mark.parametrize("s,expected", [
        ("(94,052)",   -94052.0),
        ("(1,234.5)",  -1234.5),
        ("(143.8%)",   -143.8),
        ("(66,061)",   -66061.0),
    ])
    def test_parenthetical_negatives(self, s, expected):
        assert parse_number(s) == pytest.approx(expected)

    @pytest.mark.parametrize("s,expected", [
        # pdfplumber renders "( 94,052)" with space inside parens
        ("( 94,052)",  -94052.0),
        ("( 116,245)", -116245.0),
        ("( 1,471)",   -1471.0),
    ])
    def test_parenthetical_with_internal_space(self, s, expected):
        """Space inside parenthetical negative — pdfplumber artifact."""
        assert parse_number(s) == pytest.approx(expected)


class TestParseNumberPercentages:
    """Percentage values."""

    @pytest.mark.parametrize("s,expected", [
        ("14.3%",    14.3),
        ("36.3%",    36.3),
        ("5.6%",     5.6),
        ("2.6%",     2.6),
        ("-3.2%",    -3.2),
        ("223.1%",   223.1),
        ("100.0%",   100.0),
        ("0.0%",     0.0),
    ])
    def test_percentages(self, s, expected):
        assert parse_number(s) == pytest.approx(expected)

    @pytest.mark.parametrize("s,expected", [
        ("(143.8%)",  -143.8),
        ("(121.7%)",  -121.7),
        ("(22.3%)",   -22.3),
    ])
    def test_parenthetical_percentages(self, s, expected):
        """Parenthetical percentages appear in EBITDA margin for COVID years."""
        assert parse_number(s) == pytest.approx(expected)


class TestParseNumberCurrencySymbols:
    """Dollar sign prefix."""

    @pytest.mark.parametrize("s,expected", [
        ("$589.0",   589.0),
        ("$97.3",    97.3),
        ("$78.0",    78.0),
        ("$302.2",   302.2),
        ("-$66.0",  -66.0),   # negative prose value
    ])
    def test_dollar_prefix(self, s, expected):
        assert parse_number(s) == pytest.approx(expected)


class TestParseNumberNullish:
    """Values that should return None."""

    @pytest.mark.parametrize("s", [
        "-",
        "",
        "n.a.",
        "N/A",
        "—",
        "n/a",
        None,
    ])
    def test_none_values(self, s):
        assert parse_number(s) is None


class TestParseNumberEdgeCases:
    """Edge cases from real reports."""

    def test_year_number_parses_to_float(self):
        """Year numbers like 2019 DO parse — but _NL pattern rejects them at extraction."""
        # parse_number itself does not know context; it just converts the string
        assert parse_number("2019") == pytest.approx(2019.0)

    def test_single_digit(self):
        """Single-digit artifact from split number: parse to float but _NL rejects."""
        assert parse_number("5") == pytest.approx(5.0)

    def test_large_number_with_multiple_commas(self):
        assert parse_number("1,234,567") == pytest.approx(1234567.0)

    def test_decimal_without_thousands(self):
        assert parse_number("36.31") == pytest.approx(36.31)

    def test_whitespace_stripped(self):
        assert parse_number("  14.3%  ") == pytest.approx(14.3)

    def test_combined_negative_and_percent(self):
        # "(143.8%)" — parenthetical percent negative
        assert parse_number("(143.8%)") == pytest.approx(-143.8)

    def test_zero_string(self):
        assert parse_number("0") == pytest.approx(0.0)

    def test_zero_with_decimal(self):
        assert parse_number("0.0") == pytest.approx(0.0)

    def test_footnote_digit_does_not_parse(self):
        """Single digits "1" or "2" used as footnotes parse as numbers."""
        assert parse_number("1") == pytest.approx(1.0)
        assert parse_number("2") == pytest.approx(2.0)
