"""Semantic search over the stored embeddings — cosine similarity, in-process (numpy).

Mirrors ``keyword_index`` for the semantic half of hybrid retrieval. Vectors live in the
``embeddings`` table as float32 BLOBs and are L2-normalized at build time, so cosine reduces
to a dot product; we still normalize defensively. Adequate for a local corpus (a few hundred
documents → a few thousand chunks); no external vector DB needed.

Status: Phase 3.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from src.index.store import IndexStore


@dataclass
class VectorHit:
    chunk_id: str
    score: float          # cosine similarity in [-1, 1]; higher = better


def load_matrix(store: IndexStore) -> "tuple[list[str], np.ndarray]":
    """Load every stored embedding into ``(chunk_ids, matrix)`` with L2-normalized rows.

    Built once and reused for many queries (see ``HybridRetriever``'s cache): loading +
    normalizing all vectors is the bulk of a semantic search's cost, so amortizing it across
    queries is what keeps a warm dashboard fast. Returns ``([], empty)`` for an empty index.
    """
    rows = store.connect().execute("SELECT chunk_id, vector FROM embeddings").fetchall()
    ids = [r["chunk_id"] for r in rows]
    if not rows:
        return ids, np.zeros((0, 0), dtype="float32")
    matrix = np.vstack([np.frombuffer(r["vector"], dtype="float32") for r in rows])
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, (matrix / norms).astype("float32")


def search_matrix(
    ids: "list[str]", matrix: "np.ndarray", query_vector, *, limit: int = 50,
    allowed: "set[str] | None" = None, id_index: "dict[str, int] | None" = None,
) -> list[VectorHit]:
    """Rank a preloaded ``(ids, matrix)`` by cosine similarity to ``query_vector``, best first.

    ``matrix`` rows are assumed L2-normalized (as produced by ``load_matrix``). A query whose
    dimensionality doesn't match the matrix yields no hits (guards against querying with a
    different embedder than the index was built with).

    ``allowed`` (a set of chunk_ids) restricts ranking to those rows BEFORE the top-``limit`` cut,
    so document-level scoping draws its semantic candidates from the scoped subset rather than
    being filtered out of a global pool afterward.
    """
    if not ids or matrix.size == 0:
        return []
    q = np.asarray(query_vector, dtype="float32")
    if q.shape[0] != matrix.shape[1]:
        return []
    qn = float(np.linalg.norm(q))
    if qn > 0:
        q = q / qn

    if allowed is not None:
        # Scope BEFORE the matrix multiplication. At 100k+ chunks, computing every cosine for a
        # one-company or one-document search defeats the filter and causes avoidable UI latency.
        # The retriever already maintains ``id_index`` alongside its cached matrix, making this
        # O(scoped chunks) rather than O(entire corpus).
        if id_index is None:
            id_index = {cid: i for i, cid in enumerate(ids)}
        positions = [id_index[cid] for cid in allowed if cid in id_index]
        if not positions:
            return []
        scoped = matrix[np.asarray(positions, dtype=np.intp)]
        sims = scoped @ q
        # Top-`limit` by similarity, descending; stable tie-break by chunk_id for determinism.
        order = sorted(
            range(len(positions)),
            key=lambda j: (-float(sims[j]), ids[positions[j]]),
        )[:limit]
        return [
            VectorHit(chunk_id=ids[positions[j]], score=float(sims[j])) for j in order
        ]

    sims = matrix @ q
    order = sorted(range(len(ids)), key=lambda i: (-float(sims[i]), ids[i]))[:limit]
    return [VectorHit(chunk_id=ids[i], score=float(sims[i])) for i in order]


def similarities(
    ids: "list[str]", matrix: "np.ndarray", query_vector, chunk_ids,
    *, id_index: "dict[str, int] | None" = None,
) -> "dict[str, float]":
    """Cosine similarity of ``query_vector`` to each requested ``chunk_ids`` row, as a map.

    Reuses the SAME preloaded ``(ids, matrix)`` the retriever caches for :func:`search_matrix`
    (rows assumed L2-normalized), so the analog reranker can score its candidate chunks against
    the FULL query without a second matrix load. ``id_index`` (a prebuilt ``{chunk_id: row}``)
    lets a long-lived caller avoid rebuilding the lookup each query. Chunk-ids absent from the
    matrix, or a query whose dimensionality doesn't match, are simply omitted (``{}`` for the
    latter) — a missing sim degrades the reranker to its lexical tiebreak, never crashes it.
    """
    if not ids or matrix.size == 0:
        return {}
    q = np.asarray(query_vector, dtype="float32")
    if q.shape[0] != matrix.shape[1]:
        return {}
    qn = float(np.linalg.norm(q))
    if qn > 0:
        q = q / qn
    if id_index is None:
        id_index = {cid: i for i, cid in enumerate(ids)}
    out: dict[str, float] = {}
    for cid in chunk_ids:
        i = id_index.get(cid)
        if i is not None:
            out[cid] = float(matrix[i] @ q)
    return out


def search(store: IndexStore, query_vector, *, limit: int = 50) -> list[VectorHit]:
    """Rank stored chunk embeddings by cosine similarity to ``query_vector``, best first.

    Convenience wrapper that loads the matrix per call (``load_matrix`` + ``search_matrix``).
    Callers running many queries should cache ``load_matrix`` and use ``search_matrix``.
    """
    ids, matrix = load_matrix(store)
    return search_matrix(ids, matrix, query_vector, limit=limit)
