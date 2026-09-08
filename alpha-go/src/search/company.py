"""Company card — the AlphaSense-style overview of one issuer, derived only from the index.

Everything here is a deterministic read of ``IndexStore`` + the checked-in BMV catalog /
coverage contract (``configs/bmv_corpus.yaml``); no market data, no network, no LLM. The panel
(``app.components.panels._render_company_card``) renders it beside the document reader.

Pieces:
* :func:`company_profile` — identity (name, ticker, industry), what the corpus holds for the
  company (documents by type, filings vs news, periods, latest period, languages) and its most
  recent event-type documents.
* :func:`coverage_items` — the company against the 50×50 coverage contract (filings, news,
  quarterlies, annuals).
* :func:`expected_reports` — the BMV reporting calendar: the next quarterly/annual deadlines
  with "received" / "expected" / "missing" status for this company. Deadlines are the typical
  statutory windows (quarterlies ~20 business days after quarter end, annual by 30 April), so
  they are labelled *estimated* in the UI.
* :func:`trend_change` — the sparkline caption: last period's mentions vs the previous one.
* :func:`companies_in_results` — the "other companies in these results" table.
"""
from __future__ import annotations

import datetime as _dt
import re
from collections import Counter
from dataclasses import dataclass, field

from src.shared.report_index import period_sort_key

_NEWS_TYPES = frozenset({"news_article"})
_EVENT_TYPES = ("relevant_event", "press_release", "shareholder_meeting")
_QUARTER_RE = re.compile(r"^(\d{4})-([1-4])T$")
_ANNUAL_RE = re.compile(r"^(\d{4})-FY$")
_DATE_IN_ID = re.compile(r"(?<!\d)(20\d{2})-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])(?!\d)")

# Typical BMV filing windows (month, day) after the period closes. Quarterlies are due within
# 20 business days of quarter end (≈ the 28th of the following month); the 4T report and the
# annual report both land by end-April/February respectively.
_QUARTER_DEADLINE = {1: (4, 28), 2: (7, 28), 3: (10, 28), 4: (2, 28)}   # 4T → Feb next year
_ANNUAL_DEADLINE = (4, 30)


@dataclass
class EventDoc:
    doc_id: str
    title: str
    doc_type: str
    date: str | None          # YYYY-MM-DD when derivable from the id, else None
    period: str | None


@dataclass
class CompanyProfile:
    slug: str
    name: str
    ticker: str | None
    industry: str | None
    docs_total: int = 0
    docs_by_type: dict = field(default_factory=dict)
    filings_total: int = 0
    news_total: int = 0
    periods: list = field(default_factory=list)          # chronological canonical labels
    latest_period: str | None = None
    quarterlies: int = 0
    annuals: int = 0
    languages: dict = field(default_factory=dict)
    recent_events: list = field(default_factory=list)   # EventDoc, newest first


@dataclass
class CoverageItem:
    label: str
    have: int
    target: int

    @property
    def ratio(self) -> float:
        return min(1.0, self.have / self.target) if self.target else 1.0

    @property
    def met(self) -> bool:
        return self.have >= self.target


@dataclass
class ExpectedReport:
    period: str               # "2026-3T" / "2026-FY"
    deadline: _dt.date        # estimated statutory deadline
    status: str               # "received" | "expected" | "missing"


def catalog_entry(catalog: "list[dict] | None", slug: str) -> dict:
    for entry in catalog or []:
        if str(entry.get("slug")) == slug:
            return dict(entry)
    return {}


def _event_date(doc_id: str) -> str | None:
    m = _DATE_IN_ID.search(doc_id)
    return "-".join(m.groups()) if m else None


def _language_counts(store, slug: str) -> Counter:
    """Language mix straight from the ``documents`` table (``document_rows`` omits the column)."""
    try:
        rows = store.connect().execute(
            "SELECT d.language AS language, COUNT(*) AS n FROM documents d "
            "JOIN document_companies dc ON dc.doc_id = d.doc_id "
            "WHERE dc.company = ? AND d.language IS NOT NULL GROUP BY d.language", (slug,),
        ).fetchall()
    except Exception:  # noqa: BLE001 — test stand-ins have no connection; the mix is optional
        return Counter()
    return Counter({r["language"]: r["n"] for r in rows})


def company_profile(store, slug: str, *, catalog: "list[dict] | None" = None,
                    display_name: "str | None" = None, max_events: int = 6) -> CompanyProfile:
    """Everything the index knows about one company (memberships included)."""
    entry = catalog_entry(catalog, slug)
    rows = store.document_rows(
        "EXISTS (SELECT 1 FROM document_companies dc WHERE dc.doc_id = documents.doc_id "
        "AND dc.company = ?)", [slug])
    by_type: Counter = Counter()
    langs: Counter = Counter()
    periods: set = set()
    events: list[EventDoc] = []
    for r in rows:
        dt = r["doc_type"] or "unknown"
        by_type[dt] += 1
        if "language" in r.keys() and r["language"]:
            langs[r["language"]] += 1
        if r["period"]:
            periods.add(r["period"])
        if dt in _EVENT_TYPES:
            events.append(EventDoc(doc_id=r["doc_id"], title=" ".join((r["title"] or "").split()),
                                   doc_type=dt, date=_event_date(r["doc_id"]), period=r["period"]))
    if not langs:
        langs = _language_counts(store, slug)
    news = sum(n for t, n in by_type.items() if t in _NEWS_TYPES)
    ordered = sorted(periods, key=period_sort_key)
    canonical = [p for p in ordered if _QUARTER_RE.match(p) or _ANNUAL_RE.match(p)]
    events.sort(key=lambda e: (e.date or "", e.period or ""), reverse=True)
    industry = entry.get("industry")
    if industry is None:
        for r in rows:
            if "industry" in r.keys() and r["industry"]:
                industry = r["industry"]
                break
    return CompanyProfile(
        slug=slug,
        name=str(entry.get("company") or display_name or slug.replace("_", " ").upper()),
        ticker=(str(entry["ticker"]) if entry.get("ticker") else None),
        industry=industry,
        docs_total=len(rows),
        docs_by_type=dict(by_type.most_common()),
        filings_total=len(rows) - news,
        news_total=news,
        periods=ordered,
        latest_period=(canonical[-1] if canonical else (ordered[-1] if ordered else None)),
        quarterlies=sum(1 for p in periods if _QUARTER_RE.match(p)),
        annuals=sum(1 for p in periods if _ANNUAL_RE.match(p)),
        languages=dict(langs.most_common()),
        recent_events=events[:max_events],
    )


def coverage_items(profile: CompanyProfile, contract: "dict | None") -> list[CoverageItem]:
    """The company against the coverage contract (``configs/bmv_corpus.yaml`` top-level keys)."""
    c = contract or {}
    return [
        CoverageItem("Filings & disclosures", profile.filings_total,
                     int(c.get("target_documents_per_company", 50))),
        CoverageItem("News", profile.news_total,
                     int(c.get("target_news_documents_per_company", 50))),
        CoverageItem("Quarterly reports", profile.quarterlies, int(c.get("minimum_quarterlies", 4))),
        CoverageItem("Annual reports", profile.annuals, int(c.get("minimum_annuals", 5))),
    ]


def _quarter_deadline(year: int, quarter: int) -> _dt.date:
    month, day = _QUARTER_DEADLINE[quarter]
    return _dt.date(year + (1 if quarter == 4 else 0), month, day)


def expected_reports(periods: "list[str]", *, today: "_dt.date | None" = None,
                     horizon: int = 3, lookback: int = 2) -> list[ExpectedReport]:
    """The BMV reporting calendar around ``today`` for a company with ``periods`` on file.

    Returns the last ``lookback`` closed reporting periods (so a missing recent report is
    visible as *missing*) followed by the next ``horizon`` upcoming deadlines (*expected*).
    A period the corpus already holds is *received* regardless of its deadline.
    """
    today = today or _dt.date.today()
    have = set(periods)
    out: list[ExpectedReport] = []

    # Enumerate quarter deadlines from two years back to two years ahead, then window on today.
    candidates: list[tuple[_dt.date, str]] = []
    for year in range(today.year - 2, today.year + 2):
        for q in (1, 2, 3, 4):
            candidates.append((_quarter_deadline(year, q), f"{year}-{q}T"))
        candidates.append((_dt.date(year + 1, *_ANNUAL_DEADLINE), f"{year}-FY"))
    candidates.sort()
    past = [(d, p) for d, p in candidates if d < today]
    future = [(d, p) for d, p in candidates if d >= today]
    for d, p in past[-lookback:]:
        out.append(ExpectedReport(period=p, deadline=d,
                                  status="received" if p in have else "missing"))
    for d, p in future[:horizon]:
        out.append(ExpectedReport(period=p, deadline=d,
                                  status="received" if p in have else "expected"))
    return out


def trend_change(points: list) -> "tuple[int, int, float | None]":
    """``(last, previous, pct_change)`` of mentions across the two most recent trend periods."""
    if not points:
        return 0, 0, None
    series = sorted(((p.period, p.mentions) for p in points if p.period != "Undated"),
                    key=lambda t: period_sort_key(t[0]))
    if not series:
        return 0, 0, None
    last = series[-1][1]
    prev = series[-2][1] if len(series) > 1 else 0
    pct = ((last - prev) / prev * 100.0) if prev else None
    return last, prev, pct


def companies_in_results(specs: "dict[str, dict]") -> list[dict]:
    """Per-company roll-up of a result set: documents surfaced and literal mentions counted."""
    agg: dict[str, dict] = {}
    for doc_id, e in specs.items():
        for company in dict.fromkeys([e.get("company")] + list(e.get("companies") or [])):
            if not company:
                continue
            row = agg.setdefault(company, {"company": company, "documents": 0, "mentions": 0,
                                           "lanes": set()})
            row["documents"] += 1
            if e.get("kind") == "mention" and e.get("total_matches"):
                row["mentions"] += int(e["total_matches"])
            row["lanes"].add(e.get("kind", "mention"))
    out = []
    for row in agg.values():
        row["lanes"] = ", ".join(sorted(row["lanes"]))
        out.append(row)
    out.sort(key=lambda r: (-r["mentions"], -r["documents"], r["company"]))
    return out
