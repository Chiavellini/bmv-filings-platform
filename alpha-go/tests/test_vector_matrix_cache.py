"""Tests for the cached vector-matrix path (Phase 4).

``load_matrix`` + ``search_matrix`` must rank identically to the per-call ``search``; this is
what lets the dashboard build the matrix once and reuse it across queries.
"""
from __future__ import annotations

import pytest

from src.index import vector_index


def test_load_and_search_matrix_match_per_call_search(built_index):
    store, embedder = built_index
    qv = embedder.encode(["pricing pressure from e-commerce"])[0]

    ids, matrix = vector_index.load_matrix(store)
    assert len(ids) == matrix.shape[0] > 0

    cached = vector_index.search_matrix(ids, matrix, qv, limit=5)
    per_call = vector_index.search(store, qv, limit=5)
    assert [h.chunk_id for h in cached] == [h.chunk_id for h in per_call]
    assert [round(h.score, 6) for h in cached] == [round(h.score, 6) for h in per_call]


def test_search_matrix_dim_mismatch_returns_empty(built_index):
    store, _ = built_index
    ids, matrix = vector_index.load_matrix(store)
    assert vector_index.search_matrix(ids, matrix, [0.1, 0.2, 0.3], limit=5) == []


def test_load_matrix_empty_store(tmp_path):
    from src.index.store import IndexStore
    store = IndexStore(tmp_path / "empty.db")
    store.connect()
    store.migrate()
    ids, matrix = vector_index.load_matrix(store)
    assert ids == []
    assert vector_index.search_matrix(ids, matrix, [0.0] * 8) == []


def test_similarities_matches_search_matrix_scores(built_index):
    # The analog reranker's per-chunk cosine (vector_index.similarities) must equal the cosine
    # search_matrix reports for the SAME chunks — same stored, normalized matrix.
    store, embedder = built_index
    qv = embedder.encode(["pricing pressure from e-commerce"])[0]
    ids, matrix = vector_index.load_matrix(store)
    top = vector_index.search_matrix(ids, matrix, qv, limit=5)
    sims = vector_index.similarities(ids, matrix, qv, [h.chunk_id for h in top])
    for h in top:
        assert sims[h.chunk_id] == pytest.approx(h.score, abs=1e-6)


def test_similarities_omits_unknown_and_handles_dim_mismatch(built_index):
    store, embedder = built_index
    qv = embedder.encode(["anything"])[0]
    ids, matrix = vector_index.load_matrix(store)
    sims = vector_index.similarities(ids, matrix, qv, [ids[0], "no-such-chunk"])
    assert ids[0] in sims and "no-such-chunk" not in sims
    # Wrong query dimensionality yields an empty map (never crashes the reranker).
    assert vector_index.similarities(ids, matrix, [0.1, 0.2, 0.3], [ids[0]]) == {}
