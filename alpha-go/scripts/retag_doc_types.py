"""Retag existing documents' doc_type using the canonical taxonomy (one-shot migration).

Re-infers each manifest document's doc_type from its source filename/title via the taxonomy in
configs/alpha_go.yaml, rewrites manifest.json, and UPDATEs documents.doc_type in the index.
Legacy 'release'/'report' become 'quarterly_release'. Idempotent. Run once from the Alpha Go
checkout with its repository-local environment:

    .venv/bin/python scripts/retag_doc_types.py
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.corpus.doc_types import load_taxonomy  # noqa: E402
from src.corpus.manifest import CorpusManifest, load_manifest, save_manifest  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.shared.paths import DATA_DIR, ESTATE_BRIDGE, PROJECT_ROOT  # noqa: E402


def _signal(doc) -> str:
    """Best filename/text signal for inference: original file stem + title."""
    stem = Path(doc.pdf_path or doc.markdown_path or "").stem
    return f"{stem} {doc.title or ''}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(PROJECT_ROOT / "configs" / "alpha_go.yaml"))
    ap.add_argument("--corpus-dir", default=str(DATA_DIR / "corpus"))
    ap.add_argument("--db", default=None, help="index db path (default: config index.db_path)")
    args = ap.parse_args()

    config = yaml.safe_load(Path(args.config).read_text())
    taxonomy = load_taxonomy(config)
    corpus_dir = Path(args.corpus_dir)

    db_path = Path(args.db) if args.db else ESTATE_BRIDGE.alpha_go_index_path
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    # 1) Rewrite the manifest.
    manifest = load_manifest(corpus_dir)
    before, after = Counter(), Counter()
    for doc in manifest.documents:
        before[doc.doc_type] += 1
        doc.doc_type = taxonomy.infer(_signal(doc))
        after[doc.doc_type] += 1
    save_manifest(CorpusManifest(documents=manifest.documents), corpus_dir)

    # 2) Update the index in place (no re-embed).
    store = IndexStore(db_path)
    conn = store.connect()
    idx_before = Counter(
        r["doc_type"] for r in conn.execute("SELECT doc_type FROM documents").fetchall())
    for doc in manifest.documents:
        conn.execute("UPDATE documents SET doc_type=? WHERE doc_id=?",
                     (doc.doc_type, doc.doc_id))
    store.commit()
    idx_after = Counter(
        r["doc_type"] for r in conn.execute("SELECT doc_type FROM documents").fetchall())
    store.close()

    print(f"manifest: {dict(before)} -> {dict(after)}")
    print(f"index   : {dict(idx_before)} -> {dict(idx_after)}")


if __name__ == "__main__":
    main()
