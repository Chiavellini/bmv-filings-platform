"""Rigorous cross-company acceptance audit for the Alpha Go dashboard search contract.

The audit distinguishes three things that must never be conflated:

* corpus size (documents/words available for a company),
* literal Command-F mentions (the user's exact phrase), and
* related-wording discovery (reviewable metric aliases across English/Spanish).

It tests every indexed company, checks literal counts against an independent ``rg`` oracle for
single-token queries, verifies Search/Trends agreement, verifies that keyword retrieval surfaces
documents whenever related wording exists, records latency, and reports normalized coverage.  It
is read-only: the live index and corpus are never modified.

Example:
    python scripts/audit_dashboard_search.py \
      --db data/index/alpha_go_expanded_hashing.db \
      --json outputs/audits/dashboard_search.json
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.components.doc_matches import find_matches  # noqa: E402
from src.index.keyword_index import metric_synonym_phrases, query_phrase  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.search.filters import SearchFilters  # noqa: E402
from src.search.retriever import HybridRetriever  # noqa: E402
from src.search.trends import mention_trend  # noqa: E402

DEFAULT_QUERIES = (
    "revenue", "ebitda", "net income", "operating income", "gross profit",
    "capex", "fx", "cash", "assets", "liabilities", "equity", "volume",
    "stores", "clients",
)
UNIVERSAL_QUERIES = frozenset(DEFAULT_QUERIES[:12])
ORACLE_QUERIES = frozenset(("fx", "ebitda", "capex"))
TREND_QUERIES = frozenset(("fx", "revenue", "capex"))
_WORDS = re.compile(r"\w+", re.UNICODE)


def percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    pos = max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))
    return ordered[pos]


def _rg_count(paths: list[str], literal: str) -> int | None:
    """Independent raw-file count for a single token; ``None`` when rg is unavailable."""
    if not shutil.which("rg") or not literal or any(ch.isspace() for ch in literal):
        return None
    pattern = rf"\b{re.escape(literal)}\b"
    proc = subprocess.run(
        ["rg", "-i", "-o", "--no-filename", "-e", pattern, *paths],
        text=True, capture_output=True, check=False,
    )
    # rg returns 1 when there are no matches; other non-zero statuses are real failures.
    if proc.returncode not in (0, 1):
        raise RuntimeError(proc.stderr.strip() or f"rg failed with status {proc.returncode}")
    return len(proc.stdout.splitlines())


def audit(
    db_path: Path,
    queries: tuple[str, ...] = DEFAULT_QUERIES,
    *,
    companies: "set[str] | None" = None,
) -> dict:
    started = time.perf_counter()
    store = IndexStore(db_path)
    conn = store.connect()
    retriever = HybridRetriever(store, expand_synonyms=True)  # keyword-only, production FTS path

    rows = conn.execute(
        "SELECT d.doc_id, d.markdown_path, d.language, d.period, d.source_path, "
        "       d.source_format, "
        "       (SELECT GROUP_CONCAT(dc.company) FROM document_companies dc "
        "        WHERE dc.doc_id=d.doc_id) AS companies "
        "FROM documents d ORDER BY d.doc_id"
    ).fetchall()
    if companies:
        rows = [
            row for row in rows
            if companies.intersection(
                (row["companies"] or row["doc_id"].split("/", 1)[0]).split(",")
            )
        ]
    texts: dict[str, str] = {}
    unreadable: list[str] = []
    read_started = time.perf_counter()
    for row in rows:
        try:
            texts[row["doc_id"]] = Path(row["markdown_path"]).read_text(encoding="utf-8")
        except OSError:
            texts[row["doc_id"]] = ""
            unreadable.append(row["doc_id"])
    read_seconds = time.perf_counter() - read_started

    by_company: dict[str, list] = defaultdict(list)
    for row in rows:
        memberships = (row["companies"] or row["doc_id"].split("/", 1)[0]).split(",")
        for company in memberships:
            if not companies or company in companies:
                by_company[company].append(row)

    failures: list[dict] = []
    warnings: list[dict] = []
    company_reports: list[dict] = []
    all_latencies: list[float] = []

    for company, company_rows in sorted(by_company.items()):
        paths = [row["markdown_path"] for row in company_rows]
        doc_texts = [texts[row["doc_id"]] for row in company_rows]
        doc_words = [len(_WORDS.findall(text)) for text in doc_texts]
        total_words = sum(doc_words)
        query_reports: dict[str, dict] = {}

        for query in queries:
            literal = query_phrase(query)
            related = metric_synonym_phrases(query)
            exact = concept = exact_docs = concept_docs = 0
            scan_started = time.perf_counter()
            for text in doc_texts:
                exact_spans = find_matches(text, [literal]) if literal else []
                concept_spans = find_matches(text, [literal, *related]) if literal else []
                exact += len(exact_spans)
                concept += len(concept_spans)
                exact_docs += bool(exact_spans)
                concept_docs += bool(concept_spans)
            scan_seconds = time.perf_counter() - scan_started

            retrieval_started = time.perf_counter()
            hits = retriever.search(
                query, filters=SearchFilters(companies=[company]), limit=20,
                keyword_weight=1.0, semantic_weight=0.0,
            )
            retrieval_seconds = time.perf_counter() - retrieval_started
            latency = scan_seconds + retrieval_seconds
            all_latencies.append(latency)

            oracle = _rg_count(paths, literal) if query in ORACLE_QUERIES else None
            if oracle is not None and oracle != exact:
                failures.append({
                    "kind": "exact_oracle_mismatch", "company": company, "query": query,
                    "finder": exact, "oracle": oracle,
                })
            if concept_docs and not hits:
                item = {
                    "company": company, "query": query,
                    "concept_documents": concept_docs,
                }
                if related:
                    # A curated equivalent exists in source text, so its ranked discovery lane
                    # must surface it. This is a genuine retrieval failure.
                    failures.append({"kind": "retrieval_miss", **item})
                else:
                    # The exhaustive Command-F layer deliberately uses substring semantics
                    # (e.g. cash -> cashback/cashless), whereas FTS5 ranks whole tokens. The
                    # dashboard still counts and renders every occurrence; record the tokenizer
                    # gap without misreporting a missing mention.
                    warnings.append({"kind": "fts_substring_ranking_gap", **item})

            trend_exact = None
            if query in TREND_QUERIES:
                trend_exact = sum(
                    point.mentions for point in mention_trend(
                        store, query, filters=SearchFilters(companies=[company]),
                        expand_synonyms=False,
                    )
                )
                if trend_exact != exact:
                    failures.append({
                        "kind": "trend_mismatch", "company": company, "query": query,
                        "search": exact, "trends": trend_exact,
                    })

            query_reports[query] = {
                "exact_mentions": exact,
                "exact_documents": exact_docs,
                "related_occurrences": concept,
                "related_documents": concept_docs,
                "related_terms": related,
                "retrieval_hits": len(hits),
                "trend_exact_mentions": trend_exact,
                "scan_seconds": round(scan_seconds, 4),
                "retrieval_seconds": round(retrieval_seconds, 4),
                "total_seconds": round(latency, 4),
                "rg_oracle": oracle,
            }

        universal_coverage = sum(
            query_reports[q]["related_occurrences"] > 0
            for q in queries if q in UNIVERSAL_QUERIES
        )
        universal_total = sum(q in UNIVERSAL_QUERIES for q in queries)
        concept_occurrences = sum(r["related_occurrences"] for r in query_reports.values())
        if universal_coverage < max(1, universal_total - 3):
            warnings.append({
                "kind": "low_universal_category_coverage", "company": company,
                "coverage": universal_coverage, "categories": universal_total,
            })
        median_words = statistics.median(doc_words) if doc_words else 0
        if median_words < 1_000 or median_words > 30_000:
            warnings.append({
                "kind": "abnormal_document_density", "company": company,
                "median_words_per_document": round(median_words),
            })

        company_reports.append({
            "company": company,
            "documents": len(company_rows),
            "words": total_words,
            "median_words_per_document": round(median_words),
            "languages": sorted({row["language"] for row in company_rows if row["language"]}),
            "universal_category_coverage": universal_coverage,
            "universal_category_total": universal_total,
            "related_occurrences": concept_occurrences,
            "related_occurrences_per_100k_words": round(
                concept_occurrences / max(total_words, 1) * 100_000, 2
            ),
            "queries": query_reports,
        })

    if unreadable:
        failures.append({"kind": "unreadable_documents", "doc_ids": unreadable})
    p95 = percentile(all_latencies, 0.95)
    if p95 > 2.0:
        warnings.append({"kind": "query_latency_p95", "seconds": round(p95, 4)})

    return {
        "status": "pass" if not failures else "fail",
        "database": str(db_path.resolve()),
        "documents": len(rows),
        "companies": len(by_company),
        "queries": list(queries),
        "read_seconds": round(read_seconds, 4),
        "query_latency_p50_seconds": round(percentile(all_latencies, 0.50), 4),
        "query_latency_p95_seconds": round(p95, 4),
        "elapsed_seconds": round(time.perf_counter() - started, 4),
        "failures": failures,
        "warnings": warnings,
        "company_reports": company_reports,
    }


def _print_summary(report: dict) -> None:
    print(
        f"{report['status'].upper()} · {report['documents']} documents · "
        f"{report['companies']} companies · p50 {report['query_latency_p50_seconds']:.3f}s · "
        f"p95 {report['query_latency_p95_seconds']:.3f}s"
    )
    print("company\tdocs\twords(k)\tuniversal\trelated/100k\tFX exact/related")
    for row in report["company_reports"]:
        fx = row["queries"].get("fx", {})
        print(
            f"{row['company']}\t{row['documents']}\t{row['words']/1000:.0f}\t"
            f"{row['universal_category_coverage']}/{row['universal_category_total']}\t"
            f"{row['related_occurrences_per_100k_words']:.1f}\t"
            f"{fx.get('exact_mentions', 0)}/{fx.get('related_occurrences', 0)}"
        )
    if report["failures"]:
        print("FAILURES")
        for item in report["failures"]:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))
    if report["warnings"]:
        print("WARNINGS")
        for item in report["warnings"]:
            print(json.dumps(item, ensure_ascii=False, sort_keys=True))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="index to audit (default: portable estate bridge Alpha Go index)",
    )
    parser.add_argument("--queries", help="Comma-separated query override")
    parser.add_argument("--companies", help="Comma-separated company slugs (default: all)")
    parser.add_argument("--json", type=Path, help="Optional JSON report path")
    args = parser.parse_args()
    queries = tuple(q.strip() for q in args.queries.split(",") if q.strip()) \
        if args.queries else DEFAULT_QUERIES
    companies = {c.strip() for c in args.companies.split(",") if c.strip()} \
        if args.companies else None
    if args.db is None:
        from src.shared.paths import ESTATE_BRIDGE

        db_path = ESTATE_BRIDGE.alpha_go_index_path
    else:
        db_path = args.db
    report = audit(db_path, queries, companies=companies)
    _print_summary(report)
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    raise SystemExit(0 if report["status"] == "pass" else 1)


if __name__ == "__main__":
    main()
