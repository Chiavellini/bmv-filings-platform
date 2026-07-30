#!/usr/bin/env python3
"""Expand Alpha Go to a reproducible BMV quarterly + annual-report corpus.

The BMV's official XBRL filing is the source of truth. For every under-covered company this
script downloads the newest quarterlies, extracts their narrative HTML, retains that HTML for
the in-app source reader, writes searchable UTF-8 text, and optionally adds only the new
documents to the existing certified semantic index.

Examples:
    .venv/bin/python scripts/expand_bmv_corpus.py --prepare-only --workers 4
    .venv/bin/python scripts/expand_bmv_corpus.py --index-only
    .venv/bin/python scripts/expand_bmv_corpus.py --audit
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.catalog.store import CatalogDocument, CatalogStore  # noqa: E402
from src.corpus.manifest import (  # noqa: E402
    Document, load_manifest, manifest_checksum, save_manifest,
)
from src.download.bmv_xbrl import (  # noqa: E402
    XbrlFiling, download_ticker, fetch_archive_index, load_mdna_text, parse_archive_index,
)
from src.index.build import add_document_to_index  # noqa: E402
from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.sources.base import SourceRecord  # noqa: E402
from src.sources.bmv_xbrl import BmvCompany, BmvXbrlAdapter  # noqa: E402

CATALOG_PATH = ROOT / "configs" / "bmv_corpus.yaml"
CONFIG_PATH = ROOT / "configs" / "alpha_go.yaml"
CORPUS_DIR = ROOT / "data" / "corpus"
REPORTS_DIR = ROOT.parent / "data" / "reports"
DEFAULT_DB = ROOT / "data" / "index" / "alpha_go_bmv50_final.db"
DOCUMENT_CATALOG = ROOT / "data" / "catalog" / "documents.db"
MIN_TEXT_CHARS = 2_000


@dataclass(frozen=True)
class CompanySpec:
    ticker: str
    slug: str
    company: str
    industry: str


def load_catalog() -> tuple[int, int, int, list[CompanySpec]]:
    raw = yaml.safe_load(CATALOG_PATH.read_text(encoding="utf-8")) or {}
    specs = [CompanySpec(**row) for row in raw.get("companies", [])]
    minimum = int(raw.get("minimum_quarterlies", 4))
    minimum_annuals = int(raw.get("minimum_annuals", 5))
    minimum_annual_companies = int(raw.get("minimum_annual_companies_bmv", len(specs)))
    if len(specs) < 50:
        raise RuntimeError(f"BMV catalog has only {len(specs)} companies; expected at least 50")
    if len({s.slug for s in specs}) != len(specs) or len({s.ticker for s in specs}) != len(specs):
        raise RuntimeError("BMV catalog contains duplicate slugs or tickers")
    return minimum, minimum_annuals, minimum_annual_companies, specs


def _period_key(period: str | None) -> tuple[int, int]:
    try:
        year, quarter = str(period).split("-", 1)
        if quarter == "FY":
            return int(year), 5
        return int(year), int(quarter.removesuffix("T"))
    except (TypeError, ValueError):
        return 0, 0


def _quarterly_docs(manifest, slug: str) -> list[Document]:
    return sorted(
        [d for d in manifest.documents if d.company == slug and (d.period or "").endswith("T")],
        key=lambda d: _period_key(d.period), reverse=True,
    )


def _annual_docs(manifest, slug: str) -> list[Document]:
    return sorted(
        [d for d in manifest.documents if d.company == slug and (d.period or "").endswith("-FY")],
        key=lambda d: _period_key(d.period), reverse=True,
    )


def _filings_for(
    index: list[XbrlFiling], ticker: str, kind: str = "quarterly",
) -> list[XbrlFiling]:
    return sorted(
        [f for f in index if f.ticker.upper() == ticker.upper() and f.kind == kind],
        key=lambda f: _period_key(f.period), reverse=True,
    )


def _download_company(
    spec: CompanySpec, index: list[XbrlFiling], max_filings: int, kind: str = "quarterly",
) -> None:
    download_ticker(
        spec.ticker, REPORTS_DIR / spec.slug, index=index, max_filings=max_filings,
        kinds=frozenset({kind}), delay_ms=50, write_artifacts=True,
    )


def _download_exact_filings(spec: CompanySpec, filings: list[XbrlFiling]) -> None:
    """Download an explicit missing-filing subset, preserving all prior XBRL artefacts."""
    if not filings:
        return
    download_ticker(
        spec.ticker, REPORTS_DIR / spec.slug, filings=filings,
        delay_ms=50, write_artifacts=True,
    )


def _materialize(spec: CompanySpec, filing: XbrlFiling) -> Document | None:
    source_dir = REPORTS_DIR / spec.slug / "xbrl"
    json_path = source_dir / f"{spec.ticker}_{filing.period}.json"
    if not json_path.exists():
        return None
    text = load_mdna_text(json_path) or ""
    if len(text) < MIN_TEXT_CHARS:
        print(f"  skip thin narrative: {spec.ticker} {filing.period} ({len(text):,} chars)")
        return None
    html_path = json_path.with_name(json_path.stem + "_mdna.html")
    if not html_path.exists():
        return None

    dest_dir = CORPUS_DIR / spec.slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    is_annual = filing.kind == "annual"
    suffix = "-bmv-xbrl" if is_annual else ""
    md_dest = dest_dir / f"{filing.period}{suffix}.md"
    html_dest = dest_dir / f"{filing.period}{suffix}.html"
    kind_label = "annual" if is_annual else "quarterly"
    header = (
        f"# {filing.razon_social} — {filing.period}\n\n"
        f"Official BMV XBRL {kind_label} filing, submitted {filing.filed_date}.\n\n"
    )
    md_dest.write_text(header + text + "\n", encoding="utf-8")
    shutil.copy2(html_path, html_dest)
    digest = hashlib.sha256(html_dest.read_bytes()).hexdigest()
    rel_md = md_dest.relative_to(ROOT).as_posix()
    rel_html = html_dest.relative_to(ROOT).as_posix()
    source_record = SourceRecord(
        source="BMV XBRL", source_record_id=f"{spec.ticker}:{filing.kind}:{filing.period}",
        company=spec.slug, ticker=spec.ticker,
        doc_type="annual_report" if is_annual else "quarterly_release",
        title=f"{spec.company} {filing.period}", canonical_url=filing.zip_url,
        filed_at=filing.filed_date, period=filing.period, language="es",
        document_family_id=f"{spec.slug}:{filing.kind}:{filing.period}",
    )
    doc_id = source_record.proposed_doc_id if is_annual else f"{spec.slug}/{filing.period}"
    return Document(
        doc_id=doc_id, company=spec.slug, period=filing.period,
        doc_type=source_record.doc_type, title=source_record.title,
        source_url=filing.zip_url, pdf_path=None, markdown_path=rel_md, language="es",
        industry=spec.industry,
        memberships=[{"company": spec.slug, "industry": spec.industry}],
        extra={"source": source_record.source, "source_record_id": source_record.source_record_id,
               "ticker": spec.ticker, "filed_date": filing.filed_date,
               "legal_name": filing.razon_social,
               "document_family_id": source_record.document_family_id, "version": 1},
        source_path=rel_html, source_format="html",
        original_path=rel_html, original_format="html", content_sha256=digest,
    )


def _prepare_kind(
    manifest, specs: list[CompanySpec], index: list[XbrlFiling], *, kind: str,
    minimum: int, workers: int,
) -> None:
    docs_for = _annual_docs if kind == "annual" else _quarterly_docs
    under = [s for s in specs if len(docs_for(manifest, s.slug)) < minimum]
    print(f"{kind.title()} coverage before download: "
          f"{len(specs) - len(under)}/{len(specs)} companies ready")
    available = {f.ticker.upper() for f in index if f.kind == kind}
    downloadable = [s for s in under if s.ticker.upper() in available]
    unavailable = [s.ticker for s in under if s.ticker.upper() not in available]
    if unavailable:
        print(f"{kind.title()} source gaps (route to IR/SEC fallback): {', '.join(unavailable)}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {
            pool.submit(_download_company, spec, index, minimum, kind): spec
            for spec in downloadable
        }
        for done, future in enumerate(as_completed(jobs), 1):
            spec = jobs[future]
            try:
                future.result()
                print(f"[{done}/{len(jobs)}] downloaded {spec.ticker} {kind}")
            except Exception as exc:
                print(f"[{done}/{len(jobs)}] FAILED {spec.ticker} {kind}: "
                      f"{type(exc).__name__}: {exc}")

    by_id = {d.doc_id: d for d in manifest.documents}
    for spec in downloadable:
        have = len(docs_for(manifest, spec.slug))
        for filing in _filings_for(index, spec.ticker, kind)[:minimum]:
            # Legacy quarterly identity and new annual source identity are both deterministic.
            if kind == "quarterly":
                expected_id = f"{spec.slug}/{filing.period}"
            else:
                expected_id = SourceRecord(
                    source="BMV XBRL",
                    source_record_id=f"{spec.ticker}:{filing.kind}:{filing.period}",
                    company=spec.slug, doc_type="annual_report", title="annual",
                    canonical_url=filing.zip_url, period=filing.period,
                ).proposed_doc_id
            if expected_id in by_id:
                continue
            doc = _materialize(spec, filing)
            if doc:
                by_id[doc.doc_id] = doc
                manifest.documents.append(doc)
                have += 1
            if have >= minimum:
                break

    # Thin narratives get a wider candidate pass without weakening the minimum-text gate.
    deficient = [s for s in downloadable if len(docs_for(manifest, s.slug)) < minimum]
    for spec in deficient:
        candidates = _filings_for(index, spec.ticker, kind)[: max(8, minimum * 2)]
        _download_company(spec, index, len(candidates), kind)
        have = len(docs_for(manifest, spec.slug))
        for filing in candidates:
            doc = _materialize(spec, filing)
            if doc and doc.doc_id not in by_id:
                by_id[doc.doc_id] = doc
                manifest.documents.append(doc)
                have += 1
            if have >= minimum:
                break


def prepare(
    minimum: int, specs: list[CompanySpec], *, workers: int, minimum_annuals: int = 0,
    minimum_annual_companies: int = 0,
) -> None:
    manifest = load_manifest(CORPUS_DIR)
    quarterly_under = [s for s in specs if len(_quarterly_docs(manifest, s.slug)) < minimum]
    annual_under = [s for s in specs if len(_annual_docs(manifest, s.slug)) < minimum_annuals]
    if not quarterly_under and not annual_under:
        print("Quarterly and annual coverage targets already satisfied")
        return
    archive_cache = ROOT / "data" / "bmv" / "archive_index.html"
    index = fetch_archive_index(cache_html_path=archive_cache)
    # Persist source discovery before any network download. Interrupted runs therefore remain
    # resumable and the catalog shows what exists upstream versus what has been materialized.
    annual_adapter = BmvXbrlAdapter(
        [BmvCompany(s.ticker, s.slug, s.company, s.industry) for s in specs],
        kinds=frozenset({"annual"}), filings=index,
    )
    with CatalogStore(DOCUMENT_CATALOG) as catalog:
        catalog.upsert_many(
            CatalogDocument.from_source_record(record) for record in annual_adapter.discover()
        )
    _prepare_kind(manifest, specs, index, kind="quarterly", minimum=minimum, workers=workers)
    # Annual XBRL records are discovery-only regulatory packages: their narrative extraction is
    # empty across this archive. Real annual PDFs are acquired by the BMV issuer/IR/SEC adapters.
    save_manifest(manifest, CORPUS_DIR)
    audit(minimum, specs, manifest=manifest, fail=True, minimum_annuals=minimum_annuals,
          minimum_annual_companies=minimum_annual_companies)
    with CatalogStore(DOCUMENT_CATALOG) as catalog:
        catalog.reconcile_manifest(manifest)


def prepare_all_quarterlies(
    specs: list[CompanySpec], *, workers: int, cache_only: bool,
    batch_per_company: int | None,
) -> None:
    """Materialize every currently available official BMV XBRL quarter, resumably.

    BMV publishes a rolling five-year XBRL window (rather than every historical quarter).  This
    routine treats that upstream window as the complete official BMV source and selects *missing*
    filings directly.  Consequently a bounded batch advances to older quarters on each run,
    instead of repeatedly redownloading the newest four.
    """
    archive_cache = ROOT / "data" / "bmv" / "archive_index.html"
    if cache_only:
        if not archive_cache.exists():
            raise RuntimeError(f"cached BMV archive is missing: {archive_cache}")
        index = parse_archive_index(archive_cache.read_text(encoding="utf-8"))
        if not index:
            raise RuntimeError("cached BMV archive parsed to zero filings")
        print(f"Using cached BMV archive: {len(index)} filings")
    else:
        index = fetch_archive_index(cache_html_path=archive_cache)
    manifest = load_manifest(CORPUS_DIR)
    existing_ids = {doc.doc_id for doc in manifest.documents}
    selected: dict[str, list[XbrlFiling]] = {}
    for spec in specs:
        missing = [
            filing for filing in _filings_for(index, spec.ticker, "quarterly")
            if f"{spec.slug}/{filing.period}" not in existing_ids
        ]
        if batch_per_company is not None:
            missing = missing[:batch_per_company]
        selected[spec.slug] = missing
    total = sum(len(rows) for rows in selected.values())
    print(f"All-quarterlies queue: {total} missing official filings across {len(specs)} issuers")
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {
            pool.submit(_download_exact_filings, spec, selected[spec.slug]): spec
            for spec in specs if selected[spec.slug]
        }
        for done, future in enumerate(as_completed(jobs), 1):
            spec = jobs[future]
            try:
                future.result()
                print(f"[{done}/{len(jobs)}] downloaded {spec.ticker}: {len(selected[spec.slug])} quarters")
            except Exception as exc:
                print(f"[{done}/{len(jobs)}] FAILED {spec.ticker}: {type(exc).__name__}: {exc}")

    by_id = {doc.doc_id: doc for doc in manifest.documents}
    added = 0
    for spec in specs:
        for filing in selected[spec.slug]:
            doc = _materialize(spec, filing)
            if doc is not None and doc.doc_id not in by_id:
                by_id[doc.doc_id] = doc
                manifest.documents.append(doc)
                added += 1
    save_manifest(manifest, CORPUS_DIR)
    with CatalogStore(DOCUMENT_CATALOG) as catalog:
        catalog.reconcile_manifest(manifest)
    print(f"All-quarterlies materialized: {added} new searchable filings")


def index_new(specs: list[CompanySpec], db_path: Path, *, quiet: bool = False) -> None:
    config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    config.setdefault("index", {})["db_path"] = str(db_path)
    manifest = load_manifest(CORPUS_DIR)
    target_slugs = {s.slug for s in specs}
    store = IndexStore(db_path)
    store.connect()
    store.migrate()
    indexed = {r["doc_id"] for r in store.connect().execute("SELECT doc_id FROM documents")}
    pending = [d for d in manifest.documents if d.company in target_slugs and d.doc_id not in indexed]
    print(f"Indexing {len(pending)} new documents into {db_path}")
    if not pending:
        store.close()
        return

    expected = store.get_meta("embedding_model")
    # A manual-test target may deliberately be the offline hashing index.  Preserve that vector
    # space rather than silently mixing 384-d semantic vectors into it.
    if expected == "hashing":
        config.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(store.get_meta("embedding_dim") or 256),
        })
    embedder, model_name = get_embedder(config)
    if expected and expected != model_name:
        raise RuntimeError(f"Index model is {expected!r}, but runtime resolved {model_name!r}")
    for i, doc in enumerate(pending, 1):
        md_path = ROOT / doc.markdown_path
        stats = add_document_to_index(
            store, doc, md_path.read_text(encoding="utf-8"), config, embedder=embedder,
        )
        if not quiet:
            print(f"[{i}/{len(pending)}] {doc.doc_id}: {stats['chunks']} chunks", flush=True)
    store.set_meta("manifest_checksum", manifest_checksum(manifest))
    store.set_meta("embedding_model", model_name)
    store.set_meta("embedding_dim", str(getattr(embedder, "dim", 0)))
    store.commit()
    store.close()


def audit(
    minimum: int, specs: list[CompanySpec], *, manifest=None, fail: bool = False,
    minimum_annuals: int = 0, minimum_annual_companies: int = 0,
) -> bool:
    manifest = manifest or load_manifest(CORPUS_DIR)
    rows = []
    for spec in specs:
        docs = _quarterly_docs(manifest, spec.slug)
        originals = sum(
            bool(d.original_path and (ROOT / d.original_path).exists()) for d in docs
        )
        rows.append((spec, len(docs), originals))
    ready = sum(count >= minimum and originals >= minimum for _, count, originals in rows)
    print(f"BMV coverage: {ready}/{len(rows)} companies have >= {minimum} quarterlies + originals")
    for spec, count, originals in rows:
        state = "OK" if count >= minimum and originals >= minimum else "MISSING"
        print(f"{state:7} {spec.ticker:8} {spec.slug:14} docs={count:2} originals={originals:2}")
    ok = ready >= 50
    if minimum_annuals:
        annual_rows = []
        for spec in specs:
            docs = _annual_docs(manifest, spec.slug)
            originals = sum(
                bool(d.original_path and (ROOT / d.original_path).exists()) for d in docs
            )
            annual_rows.append((spec, len(docs), originals))
        annual_ready = sum(
            count >= minimum_annuals and originals >= minimum_annuals
            for _, count, originals in annual_rows
        )
        print(f"BMV annual coverage: {annual_ready}/{len(annual_rows)} companies have "
              f">= {minimum_annuals} annuals + originals "
              f"(source gate: {minimum_annual_companies})")
        for spec, count, originals in annual_rows:
            if count < minimum_annuals or originals < minimum_annuals:
                print(f"FALLBACK {spec.ticker:8} {spec.slug:14} annuals={count:2} "
                      f"originals={originals:2}")
        ok = ok and annual_ready >= minimum_annual_companies
    if fail and not ok:
        raise RuntimeError(f"BMV coverage gate failed: only {ready} companies are ready")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true")
    mode.add_argument("--index-only", action="store_true")
    mode.add_argument("--audit", action="store_true")
    mode.add_argument("--all-quarterlies", action="store_true",
                      help="ingest every currently available official BMV XBRL quarter")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--quiet", action="store_true", help="compact indexing output for long resumable runs")
    parser.add_argument("--use-cached-archive", action="store_true",
                        help="read data/bmv/archive_index.html instead of refreshing BMV discovery")
    parser.add_argument("--quarterly-batch-per-company", type=int,
                        help="cap an all-quarterlies run fairly; later runs continue with older missing quarters")
    args = parser.parse_args(argv)
    minimum, minimum_annuals, minimum_annual_companies, specs = load_catalog()
    if args.audit:
        return 0 if audit(minimum, specs, minimum_annuals=minimum_annuals,
                          minimum_annual_companies=minimum_annual_companies) else 1
    if args.quarterly_batch_per_company is not None and args.quarterly_batch_per_company < 1:
        parser.error("--quarterly-batch-per-company must be positive")
    if args.all_quarterlies:
        prepare_all_quarterlies(
            specs, workers=args.workers, cache_only=args.use_cached_archive,
            batch_per_company=args.quarterly_batch_per_company,
        )
        index_new(specs, args.db.resolve(), quiet=args.quiet)
        return 0
    if not args.index_only:
        prepare(minimum, specs, workers=args.workers, minimum_annuals=minimum_annuals,
                minimum_annual_companies=minimum_annual_companies)
    if not args.prepare_only:
        index_new(specs, args.db.resolve(), quiet=args.quiet)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
