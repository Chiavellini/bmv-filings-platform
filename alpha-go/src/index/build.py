"""build_index — orchestrate corpus -> chunks -> keyword + semantic index.

Pipeline:
    1. load_manifest(corpus_dir)
    2. IndexStore: connect -> migrate -> clear (wholesale rebuild)
    3. for each Document: upsert_document; read markdown; chunk_document(...); insert chunks
    4. KeywordIndex.rebuild()  (FTS5)
    5. embedder.encode(chunk texts) -> upsert_embedding (float32 BLOB)
    6. set_meta(embedding_model, embedding_dim, built_at, documents, chunks)

Deterministic and offline by default (see get_embedder's auto fallback). See ROADMAP Phase 2.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import numpy as np

from src.corpus.manifest import load_manifest, manifest_checksum
from src.index.chunker import chunk_document
from src.index.embeddings import get_embedder
from src.index.keyword_index import KeywordIndex
from src.index.store import IndexStore


def _doc_memberships(doc) -> list:
    """Corpus memberships to write into ``document_companies`` for one document.

    Uses the document's explicit ``memberships`` (multi-corpus drop-ins) when present, else
    synthesizes a single membership from the primary ``company``/``industry`` columns so every
    batch-ingested / legacy document still gets exactly one junction row.
    """
    return getattr(doc, "memberships", None) or [
        {"company": doc.company, "industry": getattr(doc, "industry", None)}]


def add_document_to_index(
    store: IndexStore,
    doc,
    markdown: str,
    config: dict,
    *,
    embedder,
    replace_doc_ids: Iterable[str] = (),
) -> dict:
    """Incrementally add (or replace) ONE document in an existing index — no clear/rebuild.

    Inserts the ``documents`` row, its chunks (``chunks`` + a per-row ``chunks_fts`` insert, since
    ``KeywordIndex.rebuild`` is wholesale-only), and their embeddings, then refreshes the ``meta``
    doc/chunk counters. Re-adding the same ``doc_id`` cleanly replaces its prior chunks/vectors.
    ``replace_doc_ids`` additionally removes obsolete aliases in the same transaction. This is
    used when a versioned estate family moves from a legacy hash-derived ID to its stable current
    search ID: old text can never remain searchable after the replacement commits.

    Reuses the same primitives as :func:`build_index`; the caller supplies an already-loaded
    ``embedder`` (e.g. the live retriever's) so no second model is loaded.
    """
    index_cfg = (config or {}).get("index", {})
    chunk_cfg = index_cfg.get("chunk", {})
    target_chars = chunk_cfg.get("target_chars", 1200)
    overlap_chars = chunk_cfg.get("overlap_chars", 150)

    conn = store.connect()
    store.migrate()
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Replace-safe: drop the current row's children and any obsolete document aliases before
        # re-inserting.  All deletes and inserts share this transaction, so an embedder failure
        # rolls back to the previously searchable version.
        clear_doc_ids = {doc.doc_id}
        clear_doc_ids.update(str(doc_id) for doc_id in replace_doc_ids if doc_id)
        for clear_doc_id in sorted(clear_doc_ids):
            old_ids = [
                r["chunk_id"]
                for r in conn.execute(
                    "SELECT chunk_id FROM chunks WHERE doc_id=?", (clear_doc_id,)
                ).fetchall()
            ]
            for cid in old_ids:
                conn.execute("DELETE FROM embeddings WHERE chunk_id=?", (cid,))
                conn.execute("DELETE FROM chunks_fts WHERE chunk_id=?", (cid,))
            conn.execute("DELETE FROM chunks WHERE doc_id=?", (clear_doc_id,))
            conn.execute("DELETE FROM document_companies WHERE doc_id=?", (clear_doc_id,))
            if clear_doc_id != doc.doc_id:
                conn.execute("DELETE FROM documents WHERE doc_id=?", (clear_doc_id,))

        store.upsert_document(doc)
        store.set_document_companies(doc.doc_id, _doc_memberships(doc))
        chunks = list(chunk_document(doc.doc_id, markdown,
                                     target_chars=target_chars, overlap_chars=overlap_chars))
        for chunk in chunks:
            store.insert_chunk(chunk)
            conn.execute("INSERT INTO chunks_fts (chunk_id, text) VALUES (?, ?)",
                         (chunk.chunk_id, chunk.text))

        dim = 0
        if chunks:
            vectors = embedder.encode([c.text for c in chunks])
            for chunk, vec in zip(chunks, vectors):
                arr = np.asarray(vec, dtype="float32")
                dim = int(arr.shape[0])
                store.upsert_embedding(chunk.chunk_id, dim, arr.tobytes())

        store.set_meta("documents", str(store.count("documents")))
        store.set_meta("chunks", str(store.count("chunks")))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"doc_id": doc.doc_id, "chunks": len(chunks), "embedding_dim": dim}


def build_index(corpus_dir: Path, db_path: Path, config: dict, *, embedder=None) -> dict:
    """Build (or rebuild) the full search index from the corpus. Returns build stats.

    Args:
        corpus_dir: corpus root holding ``manifest.json`` (Phase 1 output).
        db_path: SQLite index file to (re)create.
        config: alpha_go config; uses ``index.chunk`` sizes + ``index.embedding_backend``.
        embedder: optional pre-built embedder (dependency injection for tests); when None,
            one is chosen via ``get_embedder(config)``.
    """
    corpus_dir = Path(corpus_dir)
    config = config or {}
    index_cfg = config.get("index", {})
    chunk_cfg = index_cfg.get("chunk", {})
    target_chars = chunk_cfg.get("target_chars", 1200)
    overlap_chars = chunk_cfg.get("overlap_chars", 150)
    built_at = index_cfg.get("built_at", "")  # caller may stamp; kept out for determinism

    manifest = load_manifest(corpus_dir)

    store = IndexStore(db_path)
    store.connect()
    store.migrate()
    store.clear()

    all_chunks: list = []
    for doc in manifest.documents:
        store.upsert_document(doc)
        store.set_document_companies(doc.doc_id, _doc_memberships(doc))
        md_path = Path(doc.markdown_path)
        if not md_path.is_absolute():
            candidates = (corpus_dir / md_path, Path.cwd() / md_path)
            md_path = next((candidate for candidate in candidates if candidate.exists()), candidates[0])
        markdown = md_path.read_text(encoding="utf-8")
        for chunk in chunk_document(doc.doc_id, markdown,
                                    target_chars=target_chars, overlap_chars=overlap_chars):
            store.insert_chunk(chunk)
            all_chunks.append(chunk)
    store.commit()

    # Keyword index (FTS5).
    KeywordIndex(store).rebuild()

    # Semantic index (vectors).
    if embedder is None:
        embedder, model_name = get_embedder(config)
    else:
        model_name = getattr(embedder, "model_name", type(embedder).__name__)

    embedded = 0
    dim = 0
    if all_chunks:
        batch_size = max(1, int(index_cfg.get("embedding_batch_size", 64)))
        workers = max(1, int(index_cfg.get("embedding_build_workers", 1)))
        pool_started = False
        if workers > 1:
            starter = getattr(embedder, "start_multi_process_pool", None)
            if starter is None:
                raise RuntimeError(
                    "multiple build workers require a compatible semantic embedder"
                )
            worker_threads = max(
                1, int(index_cfg.get("embedding_build_worker_threads", 1))
            )
            starter(workers, worker_threads)
            pool_started = True
        try:
            for start in range(0, len(all_chunks), batch_size):
                batch = all_chunks[start:start + batch_size]
                vectors = embedder.encode([c.text for c in batch])
                for chunk, vec in zip(batch, vectors):
                    arr = np.asarray(vec, dtype="float32")
                    dim = int(arr.shape[0])
                    store.upsert_embedding(chunk.chunk_id, dim, arr.tobytes())
                    embedded += 1
                if start and start % (batch_size * 10) == 0:
                    print(f"Embedded {start:,}/{len(all_chunks):,} chunks…", flush=True)
        finally:
            if pool_started:
                stopper = getattr(embedder, "stop_multi_process_pool", None)
                if stopper is not None:
                    stopper()
    store.commit()

    store.set_meta("embedding_model", model_name)
    store.set_meta("embedding_dim", str(dim))
    store.set_meta("built_at", str(built_at))
    store.set_meta("documents", str(len(manifest.documents)))
    store.set_meta("chunks", str(len(all_chunks)))
    store.set_meta("manifest_checksum", manifest_checksum(manifest))
    store.commit()
    store.close()

    return {
        "documents": len(manifest.documents),
        "chunks": len(all_chunks),
        "embedded": embedded,
        "embedding_model": model_name,
        "embedding_dim": dim,
    }
