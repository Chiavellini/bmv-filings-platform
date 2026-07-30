"""search_demo — run a hybrid search against the built index from the command line.

    python3 scripts/search_demo.py "presión de precios por comercio electrónico"
    python3 scripts/search_demo.py --company sport --limit 5 "expansión de clubes"

A thin wrapper over HybridRetriever for eyeballing retrieval quality on the real corpus.
Uses whatever embedder ``configs/alpha_go.yaml`` selects (sentence-transformers if installed,
else the offline hashing fallback) — the same one used to build the index.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("query")
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--db", default=None,
                    help="index db (default: the config's index.db_path)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--company", action="append", default=[], help="restrict to slug (repeatable)")
    ap.add_argument("--industry", action="append", default=[], help="restrict to sector (repeatable)")
    ap.add_argument("--doc-type", action="append", default=[], help="restrict to doc_type (repeatable)")
    args = ap.parse_args()

    from src.index.store import IndexStore
    from src.search.filters import SearchFilters
    from src.search.retriever import HybridRetriever
    from src.shared.paths import ESTATE_BRIDGE

    config = yaml.safe_load(Path(args.config).read_text())
    # Search the SAME index the app serves: the config's db, unless --db overrides.
    db = Path(args.db) if args.db else ESTATE_BRIDGE.alpha_go_index_path
    store = IndexStore(db)
    store.connect()

    retriever = HybridRetriever(store, config=config)
    filters = SearchFilters(
        companies=args.company, industries=args.industry, doc_types=args.doc_type,
    )
    hits = retriever.search(args.query, filters=None if filters.is_empty() else filters,
                            limit=args.limit)

    from src.qa.sentiment import score_sentiment

    print(f'query: "{args.query}"  ->  {len(hits)} hits  (model={store.get_meta("embedding_model")})\n')
    for i, h in enumerate(hits, 1):
        preview = h.snippet.text.replace("\n", " ").strip()
        sent = score_sentiment(h.snippet.text)
        print(f"{i:>2}. [{h.company} {h.period}] score={h.score:.4f}  "
              f"sentiment={sent.label} ({sent.rationale})")
        print(f"    {preview[:200]}")


if __name__ == "__main__":
    main()
