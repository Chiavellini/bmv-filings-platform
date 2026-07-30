"""Mention trends — how often (and in what tone) a term appears per company per quarter.

The exhaustive finder scans full indexed documents with the exact same literal + synonym phrase
set as Search. This deliberately avoids the former FTS-only mismatch where a concept could appear
in Search yet yield "No mentions found" in Trends.
"""
from __future__ import annotations

import functools
import re
from dataclasses import dataclass

from app.components.doc_matches import find_matches
from src.index.keyword_index import bilingual_search_phrases, query_phrase, search_phrases
from src.index.store import IndexStore
from src.search.mention_analytics import mention_tone


@dataclass
class TrendPoint:
    company: str
    period: str          # canonical label (e.g. "2024-1T"), derived quarter, or "Undated"
    mentions: int        # exact occurrences matching the expanded phrases
    sentiment: float     # mean lexicon score around those occurrences, in [-1, 1]
    positive: int = 0
    neutral: int = 0
    negative: int = 0


@functools.lru_cache(maxsize=1024)
def _document_text(markdown_path: str) -> str:
    try:
        return open(markdown_path, encoding="utf-8").read()
    except OSError:
        return ""


def _trend_period(period: str | None, doc_id: str) -> str:
    """Return a usable trend bucket without discarding undated searchable documents.

    Relevant-event IDs carry their publication date even though they have no reporting period,
    for example ``gmxt/relevant_event/2024-10-23t08-57/...``. Bucket those documents into the
    corresponding calendar quarter. Truly undated uploads remain visible in an explicit bucket
    so Search and Trends always count the same in-scope documents.
    """
    if period:
        return period
    match = re.search(r"(?<!\d)(20\d{2})-(0[1-9]|1[0-2])-\d{2}(?!\d)", doc_id)
    if match:
        year, month = match.groups()
        quarter = (int(month) - 1) // 3 + 1
        return f"{year}-{quarter}T"
    return "Undated"


def mention_trend(
    store: IndexStore,
    term: str,
    *,
    companies: "list[str] | None" = None,
    filters=None,
    expand_synonyms: bool = True,
) -> list[TrendPoint]:
    """Exact mentions of ``term`` per (company, period), sorted by company then period.

    Full-document scanning makes counts occurrence-based (not matching-chunk-based) and catches
    translations and phrases across line breaks. An empty/stopword-only term yields ``[]``.
    """
    literal = query_phrase(term)
    # Direct Spanish/English equivalence is part of normal Command-F behavior.  The optional
    # mode only controls broader curated related-wording aliases.
    terms = search_phrases(term) if expand_synonyms else bilingual_search_phrases(term)
    if not terms:
        return []

    clauses: list[str] = []
    params: list = []
    if filters is not None and not filters.is_empty():
        filter_where, filter_params = filters.to_sql()
        if filter_where:
            clauses.append(f"({filter_where})")
            params.extend(filter_params)
    elif companies:
        # Backward-compatible service API for CLI/tests that do not have a SearchFilters object.
        placeholders = ", ".join("?" for _ in companies)
        clauses.append(
            "EXISTS (SELECT 1 FROM document_companies dc "
            f"WHERE dc.doc_id = documents.doc_id AND dc.company IN ({placeholders}))")
        params.extend(companies)

    company_scope = set(getattr(filters, "companies", []) or (companies or []))
    industry_scope = set(getattr(filters, "industries", []) or [])
    groups: dict[tuple[str, str], list] = {}
    for row in store.document_rows(" AND ".join(clauses), params):
        text = _document_text(row["markdown_path"])
        spans = find_matches(text, terms)
        if not spans:
            continue
        period = _trend_period(row["period"], row["doc_id"])
        doc_tones = [mention_tone(text, start, end) for start, end in spans]
        memberships = store.document_companies(row["doc_id"]) or [
            {"company": row["company"], "industry": None}
        ]
        eligible = [
            membership["company"] for membership in memberships
            if (not company_scope or membership["company"] in company_scope)
            and (not industry_scope or membership.get("industry") in industry_scope)
        ]
        for company in dict.fromkeys(eligible):
            groups.setdefault((company, period), []).extend(doc_tones)

    return [
        TrendPoint(
            company=c,
            period=p,
            mentions=len(tones),
            sentiment=round(sum(t.score for t in tones) / len(tones), 3),
            positive=sum(t.label == "positive" for t in tones),
            neutral=sum(t.label == "neutral" for t in tones),
            negative=sum(t.label == "negative" for t in tones),
        )
        for (c, p), tones in sorted(groups.items())
    ]
