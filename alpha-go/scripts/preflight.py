"""Release preflight for the certified offline alpha-go artifact.

Checks source coverage, manifest portability, and index/runtime embedding compatibility before a
demo. It never downloads or mutates the corpus/index.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import yaml

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--corpus-dir", default="data/corpus")
    ap.add_argument(
        "--db",
        default=None,
        help="index to certify (default: portable estate bridge Alpha Go index)",
    )
    args = ap.parse_args(argv)

    from src.corpus.manifest import load_manifest
    from src.index.embeddings import get_embedder
    from src.index.store import IndexStore
    from src.shared.paths import ESTATE_BRIDGE

    config = yaml.safe_load(Path(args.config).read_text(encoding="utf-8")) or {}
    manifest = load_manifest(Path(args.corpus_dir))
    sources = config.get("sources", [])
    failures: list[str] = []
    for source in sources:
        slug = source["slug"]
        floor = int(source.get("floor_year", 0) or 0)
        docs = [d for d in manifest.documents if slug in d.company_slugs()
                and (not floor or not d.period or int(d.period[:4]) >= floor)]
        if len(docs) < int(source.get("minimum_documents", 1)):
            failures.append(f"{slug}: only {len(docs)} document(s) in the certified window")
        for doc in docs:
            p = Path(doc.markdown_path)
            if not p.is_absolute():
                candidates = (p, Path(args.corpus_dir) / p, ROOT / p)
                p = next((candidate for candidate in candidates if candidate.exists()), candidates[-1])
            if not p.exists():
                failures.append(f"{doc.doc_id}: missing markdown source {p}")

    db_path = (
        Path(args.db).expanduser().resolve()
        if args.db
        else ESTATE_BRIDGE.alpha_go_index_path
    )
    store = None
    if not db_path.exists():
        failures.append(f"index database missing: {db_path}")
    else:
        store = IndexStore(db_path)
        store.connect()
        if store.count("documents") != len(manifest.documents):
            failures.append(f"index documents={store.count('documents')} manifest={len(manifest.documents)}")
    try:
        embedder, name = get_embedder(config)
        expected = store.get_meta("embedding_dim") if store else None
        if expected and int(expected) != int(embedder.dim):
            failures.append(f"embedding dimension index={expected} runtime={embedder.dim}")
        print(f"runtime model: {name} ({embedder.dim} dims)")
    except Exception as exc:
        failures.append(f"runtime model unavailable: {type(exc).__name__}: {exc}")

    if failures:
        print("PREFLIGHT FAILED")
        print("\n".join(f"- {f}" for f in failures))
        return 1
    print(f"PREFLIGHT OK: {len(manifest.documents)} manifest docs, {store.count('chunks')} indexed chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
