"""Facet values + search suggestions — everything the UI offers as a click, from the index.

The zero-typing dashboard populates its widgets (industry/company multiselects, the period
range slider, suggestion pills) from here instead of asking the user to type slugs or period
labels. Facets also carry their RELATIONSHIPS (company → industry, company → periods) so the
sidebar can cascade: each filter narrows the options of the filters below it, and the user
never sees a choice that would produce zero results. Pure service layer: SQL over
``documents`` plus the vendored synonym dictionary.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from src.index.store import IndexStore


@dataclass(frozen=True)
class DocumentChoice:
    doc_id: str
    title: str
    company: str
    period: str | None
    doc_type: str
    companies: tuple[str, ...] = ()


@dataclass
class Facets:
    companies: list[str] = field(default_factory=list)
    doc_types: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    periods: list[str] = field(default_factory=list)   # lexical order = chronological
    company_industry: dict = field(default_factory=dict)   # slug -> industry tag (or None)
    company_periods: dict = field(default_factory=dict)    # slug -> sorted period labels
    documents: list[DocumentChoice] = field(default_factory=list)

    def companies_in(self, industries: "list[str]") -> list[str]:
        """Companies belonging to the selected industries (all companies when none selected)."""
        if not industries:
            return list(self.companies)
        chosen = set(industries)
        return [c for c in self.companies if self.company_industry.get(c) in chosen]

    def periods_for(self, companies: "list[str]") -> list[str]:
        """Union of the selected companies' periods, sorted (all periods when none selected)."""
        if not companies:
            return list(self.periods)
        out: set[str] = set()
        for c in companies:
            out.update(self.company_periods.get(c, []))
        return sorted(out)

    def documents_for(
        self, companies: "list[str]", industries: "list[str]",
        period_from: str | None = None, period_to: str | None = None,
        doc_types: "list[str] | None" = None,
    ) -> list[DocumentChoice]:
        """Exact-document choices under the active company/industry/period scope."""
        company_set, industry_set = set(companies), set(industries)
        type_set = set(doc_types or [])
        out = []
        for doc in self.documents:
            memberships = set(doc.companies or (doc.company,))
            if company_set and memberships.isdisjoint(company_set):
                continue
            if industry_set and not any(
                self.company_industry.get(company) in industry_set for company in memberships
            ):
                continue
            if period_from and (doc.period is None or doc.period < period_from):
                continue
            if period_to and (doc.period is None or doc.period > period_to):
                continue
            if type_set and doc.doc_type not in type_set:
                continue
            out.append(doc)
        return out


def _distinct(store: IndexStore, column: str) -> list[str]:
    rows = store.connect().execute(
        f"SELECT DISTINCT {column} AS v FROM documents "
        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY v"
    ).fetchall()
    return [r["v"] for r in rows]


def _distinct_membership(store: IndexStore, column: str) -> list[str]:
    """Distinct company/industry values from the junction (so multi-corpus docs appear under
    each of their companies, not just the primary one)."""
    rows = store.connect().execute(
        f"SELECT DISTINCT {column} AS v FROM document_companies "
        f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY v"
    ).fetchall()
    return [r["v"] for r in rows]


def facet_values(store: IndexStore) -> Facets:
    """Every clickable filter option present in the index, plus the cascade relationships.

    Company/industry facets and the company→industry / company→periods cascade come from the
    ``document_companies`` junction JOINed to ``documents`` for the period, so a document shared
    across corpora is offered under *each* of its companies.
    """
    company_industry: dict = {}
    company_periods: dict = {}
    rows = store.connect().execute(
        "SELECT DISTINCT dc.company AS company, dc.industry AS industry, d.period AS period "
        "FROM document_companies dc JOIN documents d ON d.doc_id = dc.doc_id "
        "WHERE dc.company IS NOT NULL"
    ).fetchall()
    for r in rows:
        company_industry.setdefault(r["company"], r["industry"])
        if r["period"]:
            company_periods.setdefault(r["company"], set()).add(r["period"])

    doc_rows = store.connect().execute(
        "SELECT d.doc_id, d.title, d.company, d.period, d.doc_type, "
        "       GROUP_CONCAT(dc.company) AS companies "
        "FROM documents d LEFT JOIN document_companies dc ON dc.doc_id=d.doc_id "
        "GROUP BY d.doc_id ORDER BY d.period DESC, d.title, d.doc_id"
    ).fetchall()
    documents = [
        DocumentChoice(
            doc_id=r["doc_id"], title=r["title"] or r["doc_id"], company=r["company"],
            period=r["period"], doc_type=r["doc_type"] or "",
            companies=tuple(c for c in (r["companies"] or "").split(",") if c),
        )
        for r in doc_rows
    ]

    return Facets(
        companies=_distinct_membership(store, "company"),
        doc_types=_distinct(store, "doc_type"),
        industries=_distinct_membership(store, "industry"),
        periods=_distinct(store, "period"),
        company_industry=company_industry,
        company_periods={c: sorted(ps) for c, ps in company_periods.items()},
        documents=documents,
    )


def suggested_terms(max_n: int = 12) -> list[str]:
    """Humanized concept names from the synonym dictionary — clickable search suggestions.

    Same source ``keyword_index.synonym_phrases`` expands queries from, so a suggested chip
    is guaranteed to benefit from synonym expansion. Best-effort: any failure to load the
    dictionary yields ``[]`` (the UI simply shows no chips).
    """
    try:
        from src.extract.semantic_search import load_search_dictionary
        concepts = list(load_search_dictionary().concept_aliases)
    except Exception:  # noqa: BLE001 — suggestions are optional, never fatal
        return []
    return [c.replace("_", " ") for c in concepts[:max_n]]
