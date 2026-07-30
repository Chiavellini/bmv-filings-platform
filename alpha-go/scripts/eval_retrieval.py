"""eval_retrieval — score the retriever against the labeled query set (recall@k / MRR).

    python3 scripts/eval_retrieval.py
    python3 scripts/eval_retrieval.py --keyword-only          # isolate BM25 vs hybrid
    python3 scripts/eval_retrieval.py --db data/index/alpha_go_hashing.db

Phase-6 quality gate: run before and after any retrieval change — a lift must show here
before it is claimed (ROADMAP invariant). Uses whatever embedder the config selects; with
``--keyword-only`` the semantic half is skipped entirely.
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
    ap.add_argument("--config", default="configs/alpha_go.yaml")
    ap.add_argument("--db", default=None,
                    help="index db (default: the config's index.db_path)")
    ap.add_argument("--queries", default="eval/queries.yaml")
    ap.add_argument("--k", type=int, action="append", default=[],
                    help="recall cutoff (repeatable; default 5 and 20)")
    ap.add_argument("--keyword-only", action="store_true",
                    help="skip the semantic half (BM25 baseline)")
    args = ap.parse_args()

    from src.index.store import IndexStore
    from src.search.evaluate import evaluate, load_queries
    from src.search.retriever import HybridRetriever
    from src.shared.paths import ESTATE_BRIDGE

    queries = load_queries(ROOT / args.queries)
    if not queries:
        sys.exit(f"no labeled queries in {args.queries}")

    config = yaml.safe_load((ROOT / args.config).read_text())
    # Score the SAME index the app serves: the config's db, unless --db overrides.
    db = Path(args.db) if args.db else ESTATE_BRIDGE.alpha_go_index_path
    store = IndexStore(db)
    store.connect()
    if args.keyword_only:
        retriever = HybridRetriever(store)          # no embedder, no config → keyword-only
    else:
        retriever = HybridRetriever(store, config=config)

    ks = tuple(sorted(set(args.k))) or (5, 20)
    report = evaluate(retriever, queries, ks=ks)

    mode = "keyword-only" if args.keyword_only else "hybrid"
    print(f"{len(queries)} queries · {mode} · model={store.get_meta('embedding_model')}")
    for k in ks:
        print(f"  recall@{k}: {report.recall_at[k]:.2%}")
    print(f"  MRR:       {report.mrr:.4f}")

    misses = [r for r in report.per_query if r.first_relevant_rank is None]
    if misses:
        print(f"\n{len(misses)} miss(es):")
        for r in misses:
            print(f"  - {r.query}")


if __name__ == "__main__":
    main()
