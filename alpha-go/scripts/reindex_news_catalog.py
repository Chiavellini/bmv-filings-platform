#!/usr/bin/env python3
"""Restore locally catalogued News records after a manifest-only index rebuild.

News is intentionally kept in a separate entitlement-safe catalog rather than the filing
manifest.  A full filing rebuild therefore needs this local-only follow-up to preserve the News
document type in the main search index.  It never contacts a provider or downloads an article.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.corpus.manifest import Document  # noqa: E402
from src.index.build import add_document_to_index  # noqa: E402
from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.news.catalog import NewsCatalog  # noqa: E402


def reindex_news_catalog(db_path: str | Path, catalog_path: str | Path | None = None) -> int:
    """Restore local catalogued News into one already-built main index; never uses network."""
    db_path = Path(db_path)
    catalog_path = Path(catalog_path) if catalog_path is not None else ROOT / "data" / "news" / "catalog.db"
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    if not catalog_path.is_absolute():
        catalog_path = ROOT / catalog_path
    if not catalog_path.exists():
        print("No local News catalog to reindex")
        return 0

    cfg = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8")) or {}
    store = IndexStore(db_path)
    store.connect()
    store.migrate()
    if store.get_meta("embedding_model") == "hashing":
        cfg.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(store.get_meta("embedding_dim") or 256),
        })
    embedder, _name = get_embedder(cfg)
    indexed = 0
    with NewsCatalog(catalog_path) as catalog:
        rows = catalog.conn.execute("SELECT * FROM articles ORDER BY published_at, article_id").fetchall()
        for row in rows:
            memberships = [dict(r) for r in catalog.conn.execute(
                "SELECT company, industry FROM article_companies WHERE article_id=? ORDER BY company",
                (row["article_id"],),
            )]
            if not memberships:
                continue
            markdown_path = Path(row["markdown_path"] or "")
            if not markdown_path.exists():
                # Catalogued metadata remains searchable even if a legacy markdown artifact was moved.
                markdown = "\n\n".join(part for part in (row["title"], row["summary"]) if part)
            else:
                markdown = markdown_path.read_text(encoding="utf-8")
            metadata = json.loads(row["metadata_json"] or "{}")
            primary = memberships[0]
            doc = Document(
                doc_id=row["article_id"], company=primary["company"], period=row["published_at"][:10],
                doc_type="news_article", title=row["title"], source_url=row["canonical_url"],
                pdf_path=None, markdown_path=str(markdown_path), language=row["language"] or "unknown",
                industry=primary.get("industry"), memberships=memberships,
                extra={"news": True, "provider": row["provider"], "provider_id": row["provider_id"],
                       "publisher": row["publisher"], "published_at": row["published_at"],
                       "content_mode": row["content_mode"], **metadata},
                source_path=row["canonical_url"], source_format="news",
                content_sha256=row["content_sha256"],
            )
            add_document_to_index(store, doc, markdown, cfg, embedder=embedder)
            indexed += 1
    store.commit()
    print(f"Reindexed local News: {indexed} articles; index documents={store.count('documents')}")
    return indexed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True, help="main Alpha Go index")
    parser.add_argument("--catalog", type=Path, default=ROOT / "data" / "news" / "catalog.db")
    args = parser.parse_args(argv)
    return 0 if reindex_news_catalog(args.db, args.catalog) >= 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
