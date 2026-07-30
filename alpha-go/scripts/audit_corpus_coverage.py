#!/usr/bin/env python3
"""Report Alpha Go's 50-company × 50-document coverage contract.

The report reads an index because that is exactly what the dashboard can search. It separates
filings from News and reports each configured BMV issuer's remaining primary-document deficit.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.index.store import IndexStore  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="dashboard index to audit (default: portable estate bridge index)",
    )
    parser.add_argument("--catalog", type=Path, default=ROOT / "configs" / "bmv_corpus.yaml")
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.catalog.read_text(encoding="utf-8")) or {}
    target_companies = int(cfg.get("target_companies", 50))
    target_docs = int(cfg.get("target_documents_per_company", 50))
    target_news = int(cfg.get("target_news_documents_per_company", target_docs))
    if args.db is None:
        from src.shared.paths import ESTATE_BRIDGE

        db = ESTATE_BRIDGE.alpha_go_index_path
    else:
        db = args.db if args.db.is_absolute() else ROOT / args.db
    store = IndexStore(db)
    conn = store.connect()
    rows = {
        row["company"]: row for row in conn.execute("""
            SELECT dc.company, COUNT(DISTINCT d.doc_id) AS documents,
                   SUM(CASE WHEN d.doc_type='news_article' THEN 1 ELSE 0 END) AS news
            FROM document_companies dc JOIN documents d ON d.doc_id=dc.doc_id
            GROUP BY dc.company
        """)
    }
    requested = [str(item["slug"]) for item in cfg.get("companies", [])]
    primary_covered = 0
    news_covered = 0
    for company in requested:
        row = rows.get(company)
        docs = int(row["documents"]) if row else 0
        news = int(row["news"] or 0) if row else 0
        primary = docs - news
        primary_covered += primary >= target_docs
        news_covered += news >= target_news
        print(f"{company:14} documents={docs:3} filings={primary:3} news={news:3} "
              f"filing_deficit={max(0, target_docs - primary):3} "
              f"news_deficit={max(0, target_news - news):3}")
    print(f"Primary coverage: {primary_covered}/{len(requested)} configured companies have >= {target_docs} "
          f"filings/disclosures (target: {target_companies} companies)")
    print(f"News coverage: {news_covered}/{len(requested)} configured companies have >= {target_news} news "
          f"records (a separate target; News does not fill a filing deficit)")
    return 0 if primary_covered >= target_companies and news_covered >= target_companies else 1


if __name__ == "__main__":
    raise SystemExit(main())
