"""Tests for vector_index.search — cosine over stored embeddings (Phase 3).

Uses the deterministic HashingEmbedder (lexical-overlap semantics) so the test is offline;
a real sentence-transformers run is exercised by the opt-in `model` marker elsewhere.
"""
from __future__ import annotations

from src.index import vector_index


def test_semantic_search_ranks_relevant_chunk_first(built_index):
    store, embedder = built_index
    query_vec = embedder.encode(["pricing pressure from e-commerce competitors"])[0]
    hits = vector_index.search(store, query_vec, limit=5)
    assert hits
    text = store.connect().execute(
        "SELECT text FROM chunks WHERE chunk_id=?", [hits[0].chunk_id]
    ).fetchone()["text"].lower()
    assert "pricing pressure" in text
    # Scores are sorted descending.
    assert [h.score for h in hits] == sorted((h.score for h in hits), reverse=True)


def test_semantic_search_dim_mismatch_is_skipped(built_index):
    store, _ = built_index
    # A wrong-dimensionality query vector yields no hits rather than crashing.
    assert vector_index.search(store, [0.1, 0.2, 0.3], limit=5) == []


def test_semantic_search_empty_store(tmp_path):
    from src.index.store import IndexStore
    store = IndexStore(tmp_path / "empty.db")
    store.connect()
    store.migrate()
    assert vector_index.search(store, [0.0] * 64) == []
