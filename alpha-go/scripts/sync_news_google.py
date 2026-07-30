#!/usr/bin/env python3
"""Backfill and refresh metadata/link company news through Google News RSS.

Only a title, feed-supplied summary and canonical Google News link are stored.  Each company
membership comes from a reviewed exact legal-name/manual-alias query; publisher article bodies
are neither downloaded nor retained.  Run this periodically to add newly surfaced coverage.
"""
from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.news.aliases import CompanyAliasResolver, aliases_from_companies  # noqa: E402
from src.news.catalog import NewsCatalog  # noqa: E402
from src.news.google_news import GoogleNewsRssAdapter  # noqa: E402
from src.news.ingest import ingest_article  # noqa: E402


def _eligible_aliases(resolver: CompanyAliasResolver, selected: set[str]):
    return [alias for alias in resolver.aliases if alias.kind in {"legal_name", "manual"}
            and (not selected or alias.company in selected)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "news.yaml")
    parser.add_argument("--companies", help="comma-separated company slugs; defaults to the BMV catalog")
    parser.add_argument("--max-records", type=int, help="results per company/locale (1-100)")
    parser.add_argument("--workers", type=int, default=8,
                        help="concurrent RSS requests (default: 8)")
    parser.add_argument("--timeout", type=int, default=12,
                        help="per-feed network timeout in seconds (default: 12)")
    parser.add_argument("--db", type=Path, required=True, help="main Alpha Go index to update")
    parser.add_argument("--apply", action="store_true", help="write and index verified news records")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    news = cfg.get("news", {}) or {}
    google = news.get("google_news", {}) or {}
    if not google.get("enabled", False):
        print("Google News RSS is disabled in configs/news.yaml")
        return 0
    bmv = yaml.safe_load((ROOT / "configs" / "bmv_corpus.yaml").read_text(encoding="utf-8")) or {}
    resolver = CompanyAliasResolver(aliases_from_companies(
        bmv.get("companies", []) or [], manual=news.get("manual_aliases", []) or [],
    ))
    selected = {value.strip() for value in (args.companies or "").split(",") if value.strip()}
    aliases = _eligible_aliases(resolver, selected)
    unknown = selected - {alias.company for alias in aliases}
    if unknown:
        parser.error("no Google News-eligible alias for: " + ", ".join(sorted(unknown)))
    max_records = args.max_records or int(google.get("max_records_per_alias", 100))
    if args.workers < 1:
        parser.error("--workers must be positive")
    if args.timeout < 1:
        parser.error("--timeout must be positive")
    records = []
    failures = []
    work = [(alias, locale) for locale in google.get("locales", []) or [] for alias in aliases]

    def discover_one(alias, locale):
        return list(GoogleNewsRssAdapter(
            alias, language=locale["language"], hl=locale["hl"], gl=locale["gl"],
            ceid=locale["ceid"], max_records=max_records, timeout=args.timeout,
        ).discover())

    # Discovery is side-effect-free and safely parallel. Catalog/index mutation remains on this
    # one main thread below, which prevents multiple SQLite writers from fighting each other.
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(discover_one, alias, locale): (alias, locale) for alias, locale in work}
        for completed, future in enumerate(as_completed(jobs), 1):
            alias, locale = jobs[future]
            try:
                records.extend(future.result())
            except Exception as exc:  # individual feeds must not lose an existing corpus
                failures.append(f"{alias.company}/{locale.get('hl')}: {type(exc).__name__}: {exc}")
                print(f"Warning: {failures[-1]}", file=sys.stderr)
            if completed % max(1, args.workers) == 0 or completed == len(work):
                print(f"Discovered {completed}/{len(work)} issuer-locale feeds…", flush=True)
    if not args.apply:
        print(f"Dry run: aliases={len(aliases)} discovered={len(records)} unavailable={len(failures)}")
        return 0 if records or not failures else 2
    db_path = args.db if args.db.is_absolute() else ROOT / args.db
    index = IndexStore(db_path)
    index.connect()
    index.migrate()
    app_cfg = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8")) or {}
    if index.get_meta("embedding_model") == "hashing":
        app_cfg.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(index.get_meta("embedding_dim") or 256),
        })
    embedder, _name = get_embedder(app_cfg)
    with NewsCatalog(ROOT / news.get("catalog_path", "data/news/catalog.db")) as catalog:
        results = [ingest_article(
            article, resolver=resolver, catalog=catalog,
            corpus_dir=ROOT / news.get("corpus_dir", "data/news/corpus"), store=index,
            config=app_cfg, embedder=embedder,
        ) for article in records]
        stats = catalog.stats()
    index.commit()
    print(f"Synced={sum(bool(r['indexed']) for r in results)} memberships={sum(r['memberships'] for r in results)} "
          f"catalog_articles={stats.articles} index_docs={index.count('documents')} unavailable={len(failures)}")
    return 0 if records or not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
