#!/usr/bin/env python3
"""Reconcile the durable document catalog with the current manifest and search index."""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.store import CatalogStore  # noqa: E402
from src.corpus.manifest import load_manifest  # noqa: E402


def _indexed_doc_ids(index_path: Path | None) -> set[str]:
    if not index_path or not index_path.exists():
        return set()
    conn = sqlite3.connect(f"file:{index_path}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute("SELECT doc_id FROM documents")}
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=ROOT / "data" / "corpus")
    parser.add_argument("--catalog", type=Path,
                        default=ROOT / "data" / "catalog" / "documents.db")
    parser.add_argument("--index", type=Path,
                        default=ROOT / "data" / "index" / "alpha_go_bmv50_final.db")
    args = parser.parse_args(argv)
    manifest = load_manifest(args.corpus)
    with CatalogStore(args.catalog) as catalog:
        count = catalog.reconcile_manifest(
            manifest, indexed_doc_ids=_indexed_doc_ids(args.index)
        )
        stats = catalog.stats()
    print(f"Catalog reconciled: {count} manifest documents -> {args.catalog}")
    print(f"documents={stats.documents} memberships={stats.memberships} "
          f"statuses={stats.by_status} types={stats.by_doc_type}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
