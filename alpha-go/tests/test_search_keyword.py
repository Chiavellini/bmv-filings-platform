"""Tests for KeywordIndex.search — FTS5 BM25 + synonym expansion (Phase 3)."""
from __future__ import annotations

from src.index.keyword_index import (
    KeywordIndex,
    analog_synonym_phrases,
    fts_match_query,
    metric_synonym_phrases,
)
from src.search.retriever import HybridRetriever


def _text(store, chunk_id):
    return store.connect().execute(
        "SELECT text FROM chunks WHERE chunk_id=?", [chunk_id]
    ).fetchone()["text"]


def test_keyword_search_finds_term(built_index):
    store, _ = built_index
    hits = KeywordIndex(store).search("revenues")
    assert hits
    assert "revenue" in _text(store, hits[0].chunk_id).lower()


def test_stopword_only_query_returns_nothing(built_index):
    store, _ = built_index
    assert KeywordIndex(store).search("the of and") == []


def test_synonym_expansion_broadens_recall(built_index):
    store, _ = built_index
    ki = KeywordIndex(store)
    # 'uafida' is a Spanish alias of EBITDA; it never appears literally in the corpus.
    assert ki.search("uafida", expand_synonyms=False) == []
    expanded = ki.search("uafida", expand_synonyms=True)
    assert expanded, "synonym expansion should match the EBITDA chunk"
    assert "ebitda" in _text(store, expanded[0].chunk_id).lower()


def test_fx_expands_to_banorte_spanish_language():
    metric = metric_synonym_phrases("fx")
    assert "tipo de cambio" in metric
    assert "efecto cambiario" in metric
    assert analog_synonym_phrases("fx") == []
    assert '"tipo de cambio"' in fts_match_query("fx", expand_synonyms=True)


def test_metric_equivalent_survives_analog_cleanup(built_index, monkeypatch):
    store, _ = built_index
    retriever = HybridRetriever(store, expand_synonyms=True)
    # Even an over-aggressive analog cleanup must not remove a curated metric translation.
    monkeypatch.setattr(retriever, "_clean_analog_bands", lambda *args: ([], []))
    hits = retriever.search(
        "uafida", limit=10, keyword_weight=1.0, semantic_weight=0.0,
    )
    assert hits
    assert any("ebitda" in _text(store, hit.chunk_id).lower() for hit in hits)
