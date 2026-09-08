"""Company card data: profile, coverage, BMV reporting calendar, trend delta, results roll-up."""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

from src.search.company import (
    companies_in_results,
    company_profile,
    coverage_items,
    expected_reports,
    trend_change,
)


class _Store:
    """Stand-in: ``document_rows`` returns every row whose membership matches the slug param."""

    def __init__(self, rows):
        self._rows = rows

    def document_rows(self, where="", params=()):
        slug = params[0] if params else None
        return [r for r in self._rows if slug is None or slug in r["companies"]]


def _row(doc_id, company, period, doc_type, title="t", companies=None):
    return {"doc_id": doc_id, "company": company, "period": period, "doc_type": doc_type,
            "title": title, "markdown_path": f"/x/{doc_id}.md",
            "companies": companies or [company]}


_ROWS = [
    _row("walmex/2025-4T", "walmex", "2025-4T", "quarterly_release"),
    _row("walmex/2026-1T", "walmex", "2026-1T", "quarterly_release"),
    _row("walmex/2026-2T", "walmex", "2026-2T", "quarterly_release"),
    _row("walmex/2025-FY", "walmex", "2025-FY", "annual_report"),
    _row("walmex/relevant_event/2026-07-23t10-00/x", "walmex", None, "relevant_event",
         title="  Walmex   announces  buyback "),
    _row("walmex/relevant_event/2026-03-01t10-00/y", "walmex", None, "relevant_event", title="AGM"),
    _row("news/1", "walmex", "2026-2T", "news_article", companies=["walmex", "bimbo"]),
    _row("bimbo/2026-1T", "bimbo", "2026-1T", "quarterly_release"),
]
_CATALOG = [{"ticker": "WALMEX", "slug": "walmex", "company": "Walmart de México", "industry": "retail"}]


def test_company_profile_counts_types_periods_and_events():
    p = company_profile(_Store(_ROWS), "walmex", catalog=_CATALOG)
    assert (p.name, p.ticker, p.industry) == ("Walmart de México", "WALMEX", "retail")
    assert p.docs_total == 7 and p.news_total == 1 and p.filings_total == 6
    assert p.docs_by_type["quarterly_release"] == 3 and p.docs_by_type["relevant_event"] == 2
    assert p.periods == ["2025-4T", "2025-FY", "2026-1T", "2026-2T"]     # chronological, FY after 4T
    assert p.latest_period == "2026-2T" and p.quarterlies == 3 and p.annuals == 1
    # events newest first, dates lifted from the id, titles whitespace-collapsed
    assert [e.date for e in p.recent_events] == ["2026-07-23", "2026-03-01"]
    assert p.recent_events[0].title == "Walmex announces buyback"


def test_company_profile_unknown_slug_falls_back_to_display_name():
    p = company_profile(_Store(_ROWS), "bimbo", catalog=_CATALOG, display_name="Grupo Bimbo")
    assert p.name == "Grupo Bimbo" and p.ticker is None
    assert p.docs_total == 2          # its own quarterly + the shared news article


def test_coverage_items_use_contract_and_flag_met():
    p = company_profile(_Store(_ROWS), "walmex", catalog=_CATALOG)
    items = coverage_items(p, {"target_documents_per_company": 5, "minimum_quarterlies": 4,
                               "minimum_annuals": 1})
    by = {i.label: i for i in items}
    assert by["Filings & disclosures"].have == 6 and by["Filings & disclosures"].met
    assert by["Quarterly reports"].have == 3 and not by["Quarterly reports"].met
    assert by["Annual reports"].met
    assert by["News"].target == 50 and by["News"].ratio == 1 / 50


def test_expected_reports_window_marks_received_missing_expected():
    today = dt.date(2026, 9, 7)
    reps = expected_reports(["2026-1T", "2026-2T"], today=today, horizon=4)
    # lookback = the two most recent closed deadlines (2025-FY on 30 Apr sits between the
    # 1T and 2T quarterlies), then the upcoming ones in deadline order (the 2027-1T quarterly
    # lands two days before the 2026 annual report).
    assert [(r.period, r.status) for r in reps] == [
        ("2025-FY", "missing"),           # deadline 30 Apr 2026, not on file
        ("2026-2T", "received"),          # deadline 28 Jul, on file
        ("2026-3T", "expected"),          # 28 Oct
        ("2026-4T", "expected"),          # 28 Feb 2027
        ("2027-1T", "expected"),          # 28 Apr 2027
        ("2026-FY", "expected"),          # 30 Apr 2027
    ]
    assert reps[2].deadline == dt.date(2026, 10, 28)
    assert reps[3].deadline == dt.date(2027, 2, 28)
    assert reps[5].deadline == dt.date(2027, 4, 30)
    assert len(expected_reports([], today=today)) == 5          # default horizon 3 + lookback 2
    missing = expected_reports([], today=today)
    assert missing[0].status == "missing" and missing[1].status == "missing"


def test_trend_change_last_vs_prior_period():
    pts = [SimpleNamespace(period="2026-1T", mentions=4), SimpleNamespace(period="2025-4T", mentions=2),
           SimpleNamespace(period="Undated", mentions=99)]
    assert trend_change(pts) == (4, 2, 100.0)
    assert trend_change([SimpleNamespace(period="2026-1T", mentions=3)]) == (3, 0, None)
    assert trend_change([]) == (0, 0, None)


def test_companies_in_results_rolls_up_multi_company_docs():
    specs = {
        "walmex/a": {"kind": "mention", "company": "walmex", "total_matches": 5},
        "walmex/b": {"kind": "expanded", "company": "walmex", "total_matches": 9},   # not a mention
        "news/1": {"kind": "mention", "company": "walmex", "companies": ["walmex", "bimbo"],
                   "total_matches": 1},
    }
    rows = companies_in_results(specs)
    assert [r["company"] for r in rows] == ["walmex", "bimbo"]
    assert rows[0]["documents"] == 3 and rows[0]["mentions"] == 6 and rows[0]["lanes"] == "expanded, mention"
    assert rows[1]["documents"] == 1 and rows[1]["mentions"] == 1
