#!/usr/bin/env python3
"""Discover recent issuer news through GDELT DOC 2.0 and sync a link-only corpus.

This command makes one exact-phrase query per selected legal company name.  It stores no
publisher body; clicking a result always opens the publisher's canonical link.  Verified records
are indexed with ``doc_type='news_article'`` in the same search index as filings, so the dashboard
can select News beside quarterly releases and annual reports.  Run without ``--apply`` to inspect
discovery volume first.
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
from src.news.gdelt import GdeltNewsAdapter, NewsDiscoveryError  # noqa: E402
from src.news.ingest import ingest_article  # noqa: E402


def _company_aliases(resolver: CompanyAliasResolver, selected: set[str]):
    """Use high-precision legal/manual aliases; tickers and short names remain opt-in."""
    return [alias for alias in resolver.aliases
            if alias.kind in {"legal_name", "manual"}
            and (not selected or alias.company in selected)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=ROOT / "configs" / "news.yaml")
    parser.add_argument("--companies", help="comma-separated company slugs; defaults to the full BMV catalog")
    parser.add_argument("--timespan", help="GDELT time span, e.g. 7d or 1week")
    parser.add_argument("--max-records", type=int, help="articles per exact company phrase (1–250)")
    parser.add_argument("--db", type=Path,
                        help="main Alpha Go index to update (default: alpha_go.yaml index.db_path)")
    parser.add_argument("--catalog", type=Path,
                        help="news catalog path (default: configs/news.yaml)")
    parser.add_argument("--corpus-dir", type=Path,
                        help="news corpus directory (default: configs/news.yaml)")
    parser.add_argument("--apply", action="store_true", help="write verified metadata/link records and index them")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config.read_text(encoding="utf-8")) or {}
    news = cfg.get("news", {}) or {}
    gdelt = news.get("gdelt", {}) or {}
    if not gdelt.get("enabled", True):
        print("GDELT discovery is disabled in configs/news.yaml")
        return 0
    bmv = yaml.safe_load((ROOT / "configs" / "bmv_corpus.yaml").read_text(encoding="utf-8")) or {}
    resolver = CompanyAliasResolver(aliases_from_companies(
        bmv.get("companies", []) or [], manual=news.get("manual_aliases", []) or [],
    ))
    selected = {value.strip() for value in (args.companies or "").split(",") if value.strip()}
    aliases = _company_aliases(resolver, selected)
    configured = {alias.company for alias in aliases}
    unknown = selected - configured
    if unknown:
        parser.error(f"no GDELT-eligible alias for: {', '.join(sorted(unknown))}")
    timespan = args.timespan or gdelt.get("timespan", "14d")
    max_records = args.max_records or int(gdelt.get("max_records", 25))
    records = []
    failures = []
    for alias in aliases:
        try:
            records.extend(GdeltNewsAdapter(alias, timespan=timespan, max_records=max_records).discover())
        except NewsDiscoveryError as exc:
            failures.append(str(exc))
            print(f"Warning: {exc}", file=sys.stderr)
    if not args.apply:
        print(f"Dry run: aliases={len(aliases)} discovered={len(records)} provider_verified={len(records)} "
              f"unavailable={len(failures)}")
        return 0 if records or not failures else 2
    app_cfg = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8")) or {}
    db_path = args.db or Path(app_cfg["index"]["db_path"])
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    index = IndexStore(db_path)
    index.connect()
    index.migrate()
    # An explicit --db is often a manual-test index. Preserve its embedding space instead of
    # inserting semantic vectors into a hashing database (or vice versa).
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
    print(f"Synced={sum(bool(r['indexed']) for r in results)} memberships={sum(r['memberships'] for r in results)} "
          f"catalog_articles={stats.articles} index_docs={index.count('documents')} unavailable={len(failures)}")
    return 0 if records or not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
