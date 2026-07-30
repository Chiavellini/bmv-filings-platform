"""Tests for the SEC-EDGAR adapter's offline helpers (selection, period parse, cleanup)."""
from __future__ import annotations

import pytest

from src.download.edgar import (
    annual_period_label,
    clean_release_text,
    html_to_text,
    parse_period_label,
    select_annual_document,
    select_earnings_exhibit,
)


def test_select_earnings_exhibit_prefers_ex99():
    items = [
        ("0001-index.html", 500),
        ("tm2513336d1_6k.htm", 10274),         # small cover page
        ("tm2513336d1_ex99-1.htm", 1240512),   # the release
        ("image_0.jpg", 2894),
    ]
    assert select_earnings_exhibit(items) == "tm2513336d1_ex99-1.htm"


def test_select_earnings_exhibit_falls_back_to_largest_non_cover():
    items = [("a6kcover-june2026.htm", 10829), ("a6kqedpr.htm", 15108), ("x-index.htm", 100)]
    assert select_earnings_exhibit(items) == "a6kqedpr.htm"


def test_select_earnings_exhibit_none_when_no_html():
    assert select_earnings_exhibit([("a.txt", 9), ("b.jpg", 9)]) is None


def test_parse_period_numeric_quarter():
    assert parse_period_label("Exhibit 99.1 1Q 2025 Results April 28, 2025 …") == "2025-1T"
    assert parse_period_label("3Q 2025 Results October 28, 2025") == "2025-3T"


def test_parse_period_q4_full_year():
    # "4Q and Full Year 2024 Results" → fourth quarter of 2024.
    assert parse_period_label("Exhibit 99.1 4Q and Full Year 2024 Results February 27, 2025") == "2024-4T"


def test_parse_period_spelled_out():
    assert parse_period_label("First Quarter 2023 Results …") == "2023-1T"


def test_select_annual_document_picks_largest_body():
    # A 20-F has no EX-99.1: the report is the largest .htm that isn't the index or cover.
    items = [
        ("0001-index.htm", 500),
        ("femsa-20f_cover.htm", 8000),
        ("femsa-20f.htm", 3200000),   # the annual report body
        ("logo.jpg", 4000),
    ]
    assert select_annual_document(items) == "femsa-20f.htm"


def test_select_annual_document_none_when_no_html():
    assert select_annual_document([("a.txt", 9), ("b.jpg", 9)]) is None


def test_annual_period_from_fiscal_year_cover():
    # Reads the stated fiscal year off the cover, across the intervening date digits.
    text = "FORM 20-F  For the fiscal year ended December 31, 2024  FEMSA …"
    assert annual_period_label(text, "2025-04-30") == "2024-FY"


def test_annual_period_falls_back_to_filing_year_minus_one():
    # No fiscal-year phrase → a 20-F is filed the year after fiscal close.
    assert annual_period_label("Annual report of the company", "2025-04-30") == "2024-FY"
    assert annual_period_label("no year, no date", None) is None


def test_parse_period_none_for_non_earnings():
    assert parse_period_label("FEMSA announces a cash dividend to be paid in May.") is None


def test_clean_strips_edgar_exhibit_prefix():
    raw = "EX-99.1\n2\ntm2613197d1_ex99-1.htm\nEXHIBIT 99.1\nExhibit 99.1\n1Q 2026 Results April 30, 2026"
    cleaned = clean_release_text(raw)
    assert cleaned.startswith("1Q 2026 Results")


def test_html_to_text_cleans_and_extracts():
    html = "<html><body><p>EXHIBIT 99.1</p><p>2Q 2025 Results</p><p>Total revenues grew.</p></body></html>"
    txt = html_to_text(html)
    assert "2Q 2025 Results" in txt and "Total revenues grew." in txt


def test_fetch_document_html_preserves_raw_exhibit(monkeypatch):
    """The SEC adapter exposes raw HTML so onboarding can retain the original filing."""
    import src.download.edgar as edgar

    class _Response:
        text = "<html><body>original exhibit</body></html>"

    monkeypatch.setattr(edgar, "_get", lambda _url: _Response())
    assert edgar.fetch_document_html("1061736", "0001-02-000003", "release.htm") == _Response.text


def test_edgar_network_headers_require_local_contact(monkeypatch):
    import src.download.edgar as edgar

    monkeypatch.delenv("SEC_EDGAR_USER_AGENT", raising=False)
    with pytest.raises(RuntimeError, match="SEC_EDGAR_USER_AGENT"):
        edgar._request_headers()

    monkeypatch.setenv(
        "SEC_EDGAR_USER_AGENT",
        "bmv-filings-platform/0.1 operations@example.com",
    )
    assert edgar._request_headers() == {
        "User-Agent": "bmv-filings-platform/0.1 operations@example.com"
    }
