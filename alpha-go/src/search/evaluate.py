"""Retrieval eval — recall@k / MRR over a small labeled query set (Phase 6).

A labeled query names the company (and optionally the period) its answer lives in; a hit is
relevant when it matches. Coarse but cheap to label, and enough to (a) catch regressions and
(b) compare keyword-only vs hybrid before claiming a lift (ROADMAP invariant). Pure service
layer — ``scripts/eval_retrieval.py`` is the CLI.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class LabeledQuery:
    query: str
    companies: list[str]            # relevant if the hit's company is one of these
    period: str | None = None       # optionally also require this period


@dataclass
class QueryResult:
    query: str
    first_relevant_rank: "int | None"   # 1-based; None = no relevant hit returned


@dataclass
class EvalReport:
    recall_at: dict = field(default_factory=dict)   # k -> fraction of queries hit in top-k
    mrr: float = 0.0
    per_query: list = field(default_factory=list)   # QueryResult, worst (miss) first


def load_queries(path: "Path | str") -> list[LabeledQuery]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    out: list[LabeledQuery] = []
    for item in raw.get("queries", []):
        expect = item.get("expect", {})
        companies = expect.get("companies") or (
            [expect["company"]] if expect.get("company") else [])
        if item.get("query") and companies:
            out.append(LabeledQuery(query=item["query"], companies=companies,
                                    period=expect.get("period")))
    return out


def first_relevant_rank(hits, labeled: LabeledQuery) -> "int | None":
    for rank, h in enumerate(hits, start=1):
        if h.company in labeled.companies and (
                labeled.period is None or h.period == labeled.period):
            return rank
    return None


def evaluate(retriever, queries: list[LabeledQuery], *, ks=(5, 20),
             filters=None) -> EvalReport:
    """Run every labeled query through ``retriever.search`` and score recall@k + MRR."""
    limit = max(ks)
    results = []
    for q in queries:
        hits = retriever.search(q.query, filters=filters, limit=limit)
        results.append(QueryResult(query=q.query,
                                   first_relevant_rank=first_relevant_rank(hits, q)))

    n = len(results) or 1
    ranks = [r.first_relevant_rank for r in results]
    return EvalReport(
        recall_at={k: sum(1 for r in ranks if r is not None and r <= k) / n for k in ks},
        mrr=round(sum(1.0 / r for r in ranks if r is not None) / n, 4),
        per_query=sorted(results, key=lambda r: (r.first_relevant_rank is not None,
                                                 r.first_relevant_rank or 0)),
    )
