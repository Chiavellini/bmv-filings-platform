"""SearchFilters — scope a query to a subset of the corpus.

Mirrors AlphaSense's faceting: filter hits by company, industry, period range, doc type,
language. Applied as SQL WHERE constraints joined against the documents table.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchFilters:
    doc_ids: list[str] = field(default_factory=list)       # exact documents; empty = all
    companies: list[str] = field(default_factory=list)     # slugs; empty = all
    doc_types: list[str] = field(default_factory=list)     # empty = all
    period_from: str | None = None                         # canonical label, e.g. "2022-1T"
    period_to: str | None = None
    languages: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)    # sector tags; empty = all

    def is_empty(self) -> bool:
        return not (self.doc_ids or self.companies or self.doc_types or self.period_from
                    or self.period_to or self.languages or self.industries)

    def to_sql(self) -> tuple[str, list]:
        """Return a ``(where_clause, params)`` pair constraining the ``documents`` table.

        The clause references columns of ``documents`` (``doc_type``, ``period``,
        ``language``) and is meant to be AND-ed into a query that joins chunks to their
        document. Company/industry scope is a correlated ``EXISTS`` against the
        ``document_companies`` junction, so a document shared across corpora matches under
        *any* of its companies (all consumers of this clause have ``documents`` in scope).
        Period bounds are inclusive and rely on canonical labels (e.g. ``2022-1T``) sorting
        lexically in chronological order within a year; callers needing cross-year ranges
        should pass full labels. Returns ``("", [])`` when empty.
        """
        clauses: list[str] = []
        params: list = []

        if self.doc_ids:
            placeholders = ", ".join("?" for _ in self.doc_ids)
            clauses.append(f"documents.doc_id IN ({placeholders})")
            params.extend(self.doc_ids)
        if self.companies:
            placeholders = ", ".join("?" for _ in self.companies)
            clauses.append(
                "EXISTS (SELECT 1 FROM document_companies dc "
                f"WHERE dc.doc_id = documents.doc_id AND dc.company IN ({placeholders}))")
            params.extend(self.companies)
        if self.doc_types:
            placeholders = ", ".join("?" for _ in self.doc_types)
            clauses.append(f"documents.doc_type IN ({placeholders})")
            params.extend(self.doc_types)
        if self.languages:
            placeholders = ", ".join("?" for _ in self.languages)
            clauses.append(f"documents.language IN ({placeholders})")
            params.extend(self.languages)
        if self.industries:
            placeholders = ", ".join("?" for _ in self.industries)
            clauses.append(
                "EXISTS (SELECT 1 FROM document_companies dc "
                f"WHERE dc.doc_id = documents.doc_id AND dc.industry IN ({placeholders}))")
            params.extend(self.industries)
        if self.period_from:
            clauses.append("documents.period >= ?")
            params.append(self.period_from)
        if self.period_to:
            clauses.append("documents.period <= ?")
            params.append(self.period_to)

        return " AND ".join(clauses), params
