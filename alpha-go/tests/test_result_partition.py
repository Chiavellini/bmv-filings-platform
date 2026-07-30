"""Keyword-evidence partitioning — hits must show why they matched."""
from __future__ import annotations

from types import SimpleNamespace

from src.search.retriever import HybridRetriever

from app.components.panels import _highlight_terms, partition_hits


def _hit(kw: bool):
    return SimpleNamespace(keyword_match=kw)


def test_partition_drops_semantic_only_when_keyword_hits_exist():
    hits = [_hit(True), _hit(False), _hit(True), _hit(False)]
    shown, related_only = partition_hits(hits)
    assert [h.keyword_match for h in shown] == [True, True]
    assert related_only is False


def test_partition_falls_back_to_related_when_no_keyword_hits():
    hits = [_hit(False), _hit(False)]
    shown, related_only = partition_hits(hits)
    assert len(shown) == 2
    assert related_only is True


def test_partition_empty():
    assert partition_hits([]) == ([], True)


def test_retriever_sets_keyword_match_flag(built_index):
    store, embedder = built_index
    retriever = HybridRetriever(store, embedder=embedder)
    assert not retriever.semantic_available  # hashing overlap must never be advertised as semantics

    # A term present in the fixture corpus: FTS matches → flag True on those hits.
    hits = retriever.search("revenue", limit=5)
    assert hits and all(h.keyword_match for h in hits)
    assert all(h.match_kind in {"exact", "expanded"} for h in hits)

    # A nonsense term: hashing is explicitly lexical-only, so it must not manufacture a
    # "semantic" result lane from token-overlap vectors.
    hits = retriever.search("zzyzxq", limit=5)
    assert hits == []


def test_highlight_terms_include_synonyms():
    terms = _highlight_terms("ebitda margin")
    assert "ebitda" in terms
    assert len(terms) > 2   # synonym phrases from metric_search.yaml were appended
