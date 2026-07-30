#!/usr/bin/env python3
"""Incrementally add manifest documents that are absent from a live Alpha Go index.

Use after an acquisition batch when a full index rebuild would be wasteful or would otherwise
temporarily remove separately catalogued News.  Existing document IDs are left untouched.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.corpus.manifest import load_manifest, manifest_checksum  # noqa: E402
from src.index.build import add_document_to_index  # noqa: E402
from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="live Alpha Go index")
    parser.add_argument("--corpus", type=Path, default=ROOT / "data" / "corpus")
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "alpha_go.yaml")
    args = parser.parse_args(argv)
    db_path = args.db if args.db.is_absolute() else ROOT / args.db
    corpus_dir = args.corpus if args.corpus.is_absolute() else ROOT / args.corpus
    config_path = args.config if args.config.is_absolute() else ROOT / args.config
    manifest = load_manifest(corpus_dir)

    store = IndexStore(db_path)
    store.connect()
    store.migrate()
    existing_ids = {row["doc_id"] for row in store.connect().execute("SELECT doc_id FROM documents")}
    pending = [document for document in manifest.documents if document.doc_id not in existing_ids]
    if not pending:
        store.set_meta("manifest_checksum", manifest_checksum(manifest))
        store.commit()
        print("Index already contains every manifest document")
        return 0

    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if store.get_meta("embedding_model") == "hashing":
        config.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(store.get_meta("embedding_dim") or 256),
        })
    embedder, _name = get_embedder(config)
    for number, document in enumerate(pending, 1):
        markdown_path = Path(document.markdown_path)
        if not markdown_path.is_absolute():
            markdown_path = ROOT / markdown_path
        add_document_to_index(store, document, markdown_path.read_text(encoding="utf-8"), config,
                              embedder=embedder)
        if number % 25 == 0:
            print(f"Indexed {number}/{len(pending)} manifest documents…", flush=True)
    store.set_meta("manifest_checksum", manifest_checksum(manifest))
    store.commit()
    print(f"Indexed manifest delta: {len(pending)} documents; total={store.count('documents')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
