"""Phase-6 eval — recall@k/MRR scoring math, query loading, and the determinism gate."""
from __future__ import annotations

import shutil

from src.index.build import build_index
from src.index.embeddings import HashingEmbedder
from src.index.store import IndexStore
from src.search.evaluate import (EvalReport, LabeledQuery, evaluate, first_relevant_rank,
                                 load_queries)
from src.search.retriever import HybridRetriever

from tests.conftest import FIXTURE, _EMBED_DIM


def test_load_queries_yaml(tmp_path):
    p = tmp_path / "q.yaml"
    p.write_text(
        "queries:\n"
        "  - query: OXXO margin\n"
        "    expect: {company: femsa}\n"
        "  - query: cost pressure\n"
        "    expect: {companies: [bimbo, walmex], period: 2024-1T}\n"
        "  - query: unlabeled\n"          # no expect → skipped
        "    expect: {}\n",
        encoding="utf-8")
    qs = load_queries(p)
    assert [q.companies for q in qs] == [["femsa"], ["bimbo", "walmex"]]
    assert qs[1].period == "2024-1T"


def test_repo_query_set_loads():
    from src.shared.paths import PROJECT_ROOT
    qs = load_queries(PROJECT_ROOT / "eval" / "queries.yaml")
    assert len(qs) >= 20
    assert all(q.query and q.companies for q in qs)


def test_first_relevant_rank_company_and_period():
    class H:  # minimal Hit shape
        def __init__(self, company, period):
            self.company, self.period = company, period

    q = LabeledQuery(query="x", companies=["acme"], period="2024-2T")
    hits = [H("other", "2024-2T"), H("acme", "2024-1T"), H("acme", "2024-2T")]
    assert first_relevant_rank(hits, q) == 3
    assert first_relevant_rank(hits, LabeledQuery("x", ["acme"])) == 2
    assert first_relevant_rank([], q) is None


def test_evaluate_on_fixture_index(built_index):
    store, embedder = built_index
    retriever = HybridRetriever(store, embedder=embedder)
    report = evaluate(retriever, [
        LabeledQuery(query="revenue", companies=["acme"]),
        LabeledQuery(query="revenue", companies=["not-in-corpus"]),  # guaranteed miss
    ], ks=(5,))
    assert isinstance(report, EvalReport)
    assert report.recall_at[5] == 0.5          # one hit, one miss
    assert report.mrr == 0.5                   # first query answered at rank 1
    assert report.per_query[0].first_relevant_rank is None  # misses sort first


def test_determinism_gate(tmp_path):
    """Identical inputs → identical index contents and identical ranking (no network/LLM)."""
    rankings = []
    for run in ("a", "b"):
        corpus = tmp_path / run / "corpus"
        (corpus / "acme").mkdir(parents=True)
        for md in FIXTURE.glob("*.md"):
            shutil.copy2(md, corpus / "acme" / md.name)
        db = tmp_path / run / "alpha.db"
        config = {"index": {"embedding_backend": "hashing", "hashing_dim": _EMBED_DIM,
                            "chunk": {"target_chars": 400, "overlap_chars": 50}}}
        build_index(corpus, db, config, embedder=HashingEmbedder(dim=_EMBED_DIM))

        store = IndexStore(db)
        store.connect()
        chunks = store.connect().execute(
            "SELECT chunk_id, doc_id, char_start, char_end FROM chunks ORDER BY chunk_id"
        ).fetchall()
        retriever = HybridRetriever(store, embedder=HashingEmbedder(dim=_EMBED_DIM))
        ranking = {q: [h.chunk_id for h in retriever.search(q, limit=10)]
                   for q in ("revenue", "margin outlook", "cash flow")}
        rankings.append(([tuple(r) for r in chunks], ranking))

    assert rankings[0][0] == rankings[1][0], "chunk table differs between identical builds"
    assert rankings[0][1] == rankings[1][1], "ranking differs between identical builds"
