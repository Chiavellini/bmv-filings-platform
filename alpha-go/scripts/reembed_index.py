"""Atomically replace an existing index's vectors with the configured semantic model.

This migration preserves documents, chunks, FTS rows, source paths, and corpus memberships
byte-for-byte.  Only the ``embeddings`` table and embedding metadata change.  Work is performed
on ``<destination>.building`` and promoted with ``os.replace`` only after every chunk has a
vector, so an interruption cannot corrupt the source or leave a half-certified destination.

Example:
    python scripts/reembed_index.py \
      --source data/index/alpha_go_expanded_hashing.db \
      --destination data/index/alpha_go_expanded_semantic.db
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from pathlib import Path

import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _source_signature(path: Path) -> str:
    """Stable document-content signature guarding migration promotion against live uploads."""
    from src.index.store import IndexStore

    store = IndexStore(path)
    conn = store.connect()
    digest = hashlib.sha256()
    for row in conn.execute(
        "SELECT doc_id, COALESCE(content_sha256, ''), COALESCE(markdown_path, '') "
        "FROM documents ORDER BY doc_id"
    ):
        digest.update("\0".join(str(v) for v in row).encode("utf-8"))
        digest.update(b"\n")
    digest.update(f"chunks={store.count('chunks')}".encode("ascii"))
    store.close()
    return digest.hexdigest()


def reembed(
    source: Path, destination: Path, config: dict, *, embedder=None, resume: bool = False,
) -> dict:
    from src.index.embeddings import get_embedder
    from src.index.store import IndexStore

    threads = int((config.get("index") or {}).get("embedding_cpu_threads", 0) or 0)
    if threads:
        import torch
        torch.set_num_threads(threads)

    source = source.resolve()
    destination = destination.resolve()
    if not source.exists():
        raise FileNotFoundError(source)
    if source == destination:
        raise ValueError("Source and destination must differ; migration is intentionally copy-on-write.")

    building = destination.with_suffix(destination.suffix + ".building")
    building.parent.mkdir(parents=True, exist_ok=True)
    current_source_signature = _source_signature(source)
    if building.exists() and not resume:
        building.unlink()
    if not building.exists():
        shutil.copy2(source, building)

    store = IndexStore(building)
    conn = store.connect()
    store.migrate()
    recorded_source_signature = store.get_meta("reembed_source_signature")
    if recorded_source_signature and recorded_source_signature != current_source_signature:
        store.close()
        raise RuntimeError(
            "Source index changed after this migration checkpoint was created "
            "(for example, a dashboard upload). Keep the live source and restart the "
            "copy-on-write migration so no uploaded document can be lost."
        )
    if not recorded_source_signature:
        # Backward-compatible with checkpoints created before the drift guard was introduced.
        store.set_meta("reembed_source_signature", current_source_signature)
        store.commit()
    total = store.count("chunks")
    if total == 0:
        raise ValueError("Source index has no chunks.")

    if embedder is None:
        embedder, model_name = get_embedder(config)
    else:
        model_name = getattr(embedder, "model_name", type(embedder).__name__)
    if getattr(embedder, "semantic_quality", "semantic") == "lexical":
        raise RuntimeError("Configured backend resolved to lexical hashing, not a semantic model.")

    dim = int(embedder.dim)
    workers = max(
        1,
        int(
            (config.get("index") or {}).get(
                "embedding_migration_workers", 1
            )
        ),
    )
    if workers > 1:
        starter = getattr(embedder, "start_multi_process_pool", None)
        if starter is None:
            raise RuntimeError(
                "multiple migration workers require a compatible semantic embedder"
            )
        worker_threads = max(
            1,
            int(
                (config.get("index") or {}).get(
                    "embedding_migration_worker_threads", 1
                )
            ),
        )
        starter(workers, worker_threads)
    batch_size = max(1, int((config.get("index") or {}).get("embedding_batch_size", 256)))
    remaining = conn.execute(
        "SELECT COUNT(*) AS n FROM chunks c LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id "
        "WHERE e.chunk_id IS NULL OR e.dim != ?",
        (dim,),
    ).fetchone()["n"]
    embedded = total - int(remaining)
    print(f"Resuming at {embedded:,}/{total:,} chunks", flush=True)
    cursor = conn.execute(
        "SELECT c.chunk_id, c.text FROM chunks c "
        "LEFT JOIN embeddings e ON e.chunk_id=c.chunk_id "
        "WHERE e.chunk_id IS NULL OR e.dim != ? ORDER BY c.chunk_id",
        (dim,),
    )
    try:
        while True:
            rows = cursor.fetchmany(batch_size)
            if not rows:
                break
            vectors = embedder.encode([row["text"] for row in rows])
            payload = []
            for row, vector in zip(rows, vectors):
                arr = np.asarray(vector, dtype="float32")
                dim = int(arr.shape[0])
                payload.append((row["chunk_id"], dim, arr.tobytes()))
            conn.executemany(
                "INSERT OR REPLACE INTO embeddings (chunk_id, dim, vector) VALUES (?, ?, ?)",
                payload,
            )
            conn.commit()
            embedded += len(payload)
            print(f"Embedded {embedded:,}/{total:,} chunks", flush=True)
    finally:
        stopper = getattr(embedder, "stop_multi_process_pool", None)
        if stopper is not None:
            stopper()

    wrong_dim = conn.execute(
        "SELECT COUNT(*) AS n FROM embeddings WHERE dim != ?", (dim,)
    ).fetchone()["n"]
    if embedded != total or store.count("embeddings") != total or wrong_dim:
        raise RuntimeError(
            f"Incomplete migration: chunks={total}, embedded={store.count('embeddings')}, "
            f"wrong_dim={wrong_dim}"
        )
    store.set_meta("embedding_model", model_name)
    store.set_meta("embedding_dim", str(dim))
    store.commit()
    if _source_signature(source) != current_source_signature:
        store.close()
        raise RuntimeError(
            "Source index changed while vectors were being generated. Promotion was cancelled; "
            "the live source remains intact."
        )
    store.close()

    os.replace(building, destination)
    return {
        "source": str(source),
        "destination": str(destination),
        "documents": total and IndexStore(destination).count("documents"),
        "chunks": total,
        "embedded": embedded,
        "embedding_model": model_name,
        "embedding_dim": dim,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--config", default="configs/alpha_go.yaml")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    print(reembed(Path(args.source), Path(args.destination), config, resume=args.resume))


if __name__ == "__main__":
    main()
