#!/usr/bin/env python3
"""Sync configured, approved RSS feeds into the Alpha Go document corpus as News.

Default is a dry run.  ``--apply`` writes only metadata/permitted summaries unless the configured
source explicitly declares a licensed full-text entitlement. Verified records carry the News
document type in the main search index. Schedule this command externally (for example hourly)
after populating ``configs/news.yaml`` with approved feeds.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.news.aliases import CompanyAliasResolver, aliases_from_companies  # noqa: E402
from src.news.catalog import NewsCatalog  # noqa: E402
from src.news.ingest import ingest_article  # noqa: E402
from src.news.rss import RssNewsAdapter  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "news.yaml")
    parser.add_argument("--db", type=Path,
                        help="main Alpha Go index to update (default: alpha_go.yaml index.db_path)")
    parser.add_argument("--catalog", type=Path,
                        help="news catalog path (default: configs/news.yaml)")
    parser.add_argument("--corpus-dir", type=Path,
                        help="news corpus directory (default: configs/news.yaml)")
    parser.add_argument("--apply", action="store_true", help="write verified articles and update news index")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    news = cfg.get("news", {}) or {}
    sources = news.get("sources", []) or []
    bmv = yaml.safe_load((ROOT / "configs" / "bmv_corpus.yaml").read_text(encoding="utf-8")) or {}
    resolver = CompanyAliasResolver(aliases_from_companies(
        bmv.get("companies", []) or [], manual=news.get("manual_aliases", []) or [],
    ))
    records = []
    for source in sources:
        adapter = RssNewsAdapter(
            name=source["name"], url=source["url"], publisher=source.get("publisher") or source["name"],
            language=source.get("language", "unknown"),
            content_mode=source.get("content_mode", "metadata_link"),
        )
        records.extend(adapter.discover())
    if not args.apply:
        matched = sum(bool(resolver.resolve(article.content_text)) for article in records)
        print(f"Dry run: discovered={len(records)} verified_company_mentions={matched}")
        return 0
    app_cfg = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8")) or {}
    db_path = args.db or Path(app_cfg["index"]["db_path"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    index = IndexStore(db_path)
    index.connect()
    index.migrate()
    # Keep a manually selected hashing index internally consistent when this command is run
    # with --db; the certified index continues to use its configured multilingual model.
    if index.get_meta("embedding_model") == "hashing":
        app_cfg.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(index.get_meta("embedding_dim") or 256),
        })
    embedder, _name = get_embedder(app_cfg)
    catalog_path = args.catalog or ROOT / news.get("catalog_path", "data/news/catalog.db")
    corpus_dir = args.corpus_dir or ROOT / news.get("corpus_dir", "data/news/corpus")
    with NewsCatalog(catalog_path) as catalog:
        results = [ingest_article(
            article, resolver=resolver, catalog=catalog,
            corpus_dir=corpus_dir, store=index,
            config=app_cfg, embedder=embedder,
        ) for article in records]
        stats = catalog.stats()
    index.commit()
    print(f"Synced={sum(bool(r['indexed']) for r in results)} matched={sum(r['memberships'] for r in results)} "
          f"catalog_articles={stats.articles} index_docs={index.count('documents')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
