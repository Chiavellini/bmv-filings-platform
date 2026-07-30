"""build_index CLI — chunk the corpus and build the hybrid (FTS5 + embeddings) index.

    python3 scripts/build_index.py --config configs/alpha_go.yaml

The certified config requires the pinned local embedding model; a missing model fails loudly
instead of silently producing an incompatible index.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import yaml

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--corpus-dir", default="data/corpus")
    ap.add_argument(
        "--db",
        default=None,
        help=(
            "target index (default: the portable estate bridge's Alpha Go index)"
        ),
    )
    ap.add_argument(
        "--embedding-backend",
        choices=("hashing", "sentence-transformers"),
        help=(
            "explicit build backend; omitted preserves the certified config "
            "or an existing target index's hashing vector space"
        ),
    )
    ap.add_argument(
        "--hashing-dim",
        type=int,
        default=256,
        help="vector dimension when --embedding-backend=hashing",
    )
    args = ap.parse_args()

    from src.corpus.manifest import load_manifest
    from src.index.build import build_index
    from src.index.store import IndexStore
    from src.shared.paths import ESTATE_BRIDGE
    from scripts.reindex_news_catalog import reindex_news_catalog

    config = yaml.safe_load(Path(args.config).read_text())
    # Manual dashboard testing may deliberately use a hashing index on a host without the
    # certified Torch runtime.  Preserve that existing vector space during a rebuild instead of
    # trying to load the semantic model or silently changing the index dimension.
    target = (
        Path(args.db).expanduser().resolve()
        if args.db
        else ESTATE_BRIDGE.alpha_go_index_path
    )
    if args.embedding_backend:
        config.setdefault("index", {})["embedding_backend"] = args.embedding_backend
        if args.embedding_backend == "hashing":
            if args.hashing_dim <= 0:
                ap.error("--hashing-dim must be positive")
            config["index"].update(
                {"strict_runtime": False, "hashing_dim": args.hashing_dim}
            )
    elif target.exists():
        existing = IndexStore(target)
        existing.connect()
        existing.migrate()
        if existing.get_meta("embedding_model") == "hashing":
            config.setdefault("index", {}).update({
                "embedding_backend": "hashing", "strict_runtime": False,
                "hashing_dim": int(existing.get_meta("embedding_dim") or 256),
            })
        existing.close()
    target.parent.mkdir(parents=True, exist_ok=True)
    staging = target.with_name(f".{target.name}.building-{os.getpid()}")
    previous = target.with_name(f"{target.name}.previous")
    if staging.exists():
        raise RuntimeError(f"refusing to overwrite staging index: {staging}")

    stats = build_index(Path(args.corpus_dir), staging, config)
    # Legacy manifests excluded News and required a second local-catalog pass.
    # The shared-estate projection now includes News directly, so a second pass
    # would reintroduce a competing source of truth.
    manifest = load_manifest(Path(args.corpus_dir))
    if not any(doc.doc_type == "news_article" for doc in manifest.documents):
        reindex_news_catalog(staging)

    connection = sqlite3.connect(f"file:{staging}?mode=ro", uri=True)
    try:
        quick_check = connection.execute("PRAGMA quick_check").fetchone()[0]
        counts = {
            table: int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
            for table in ("documents", "chunks", "chunks_fts", "embeddings")
        }
        metadata = dict(connection.execute("SELECT key,value FROM meta"))
    finally:
        connection.close()
    if quick_check != "ok":
        raise RuntimeError(f"staging index quick_check failed: {quick_check}")
    if counts["documents"] != len(manifest.documents):
        raise RuntimeError(
            "staging index document count mismatch: "
            f"{counts['documents']} != {len(manifest.documents)}"
        )
    if len({counts["chunks"], counts["chunks_fts"], counts["embeddings"]}) != 1:
        raise RuntimeError(f"staging index row counts disagree: {counts}")
    if metadata.get("embedding_model") != stats["embedding_model"]:
        raise RuntimeError("staging index embedding metadata does not match build result")

    replaced_existing = target.exists()
    try:
        if replaced_existing:
            os.replace(target, previous)
        os.replace(staging, target)
    except Exception:
        if replaced_existing and previous.exists() and not target.exists():
            os.replace(previous, target)
        raise
    print(
        {
            **stats,
            "db_path": str(target),
            "previous_path": str(previous) if replaced_existing else None,
            "sqlite_quick_check": quick_check,
        }
    )


if __name__ == "__main__":
    main()
