#!/usr/bin/env python3
"""Discover and optionally materialize official BMV issuer-page documents.

The default pass discovers narrative annual-report PDFs for the configured BMV universe and
writes them to the durable catalog. ``--download`` preserves originals, extracts text and adds
accepted documents to the corpus manifest. Relevant-event discovery is opt-in because it is a
much larger rolling corpus and should be reviewed before materialization.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from src.catalog.store import CatalogDocument, CatalogStore  # noqa: E402
from src.corpus.manifest import (  # noqa: E402
    CorpusManifest, Document, load_manifest, manifest_checksum, save_manifest,
)
from src.download.downloader import _make_session, _session_get  # noqa: E402
from src.index.build import add_document_to_index  # noqa: E402
from src.index.embeddings import get_embedder  # noqa: E402
from src.index.store import IndexStore  # noqa: E402
from src.sources.base import SourceRecord  # noqa: E402
from src.sources.bmv_issuer import (  # noqa: E402
    BmvIssuer, fetch_issuer_ids, parse_events_page, parse_financial_page,
)

CONFIG_PATH = ROOT / "configs" / "bmv_corpus.yaml"
CATALOG_PATH = ROOT / "data" / "catalog" / "documents.db"
CORPUS_DIR = ROOT / "data" / "corpus"
RAW_DIR = ROOT / "data" / "raw" / "bmv_issuer"
DERIVED_DIR = ROOT / "data" / "derived"
MIN_TEXT_CHARS = 2_000


def load_issuers() -> list[BmvIssuer]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    ids = fetch_issuer_ids()
    missing = [row["ticker"] for row in cfg.get("companies", [])
               if row["ticker"].upper() not in ids]
    if missing:
        raise RuntimeError("BMV issuer directory has no IDs for: " + ", ".join(missing))
    return [
        BmvIssuer(row["ticker"], ids[row["ticker"].upper()], row["company"], row["slug"],
                  row.get("industry"))
        for row in cfg.get("companies", [])
    ]


def _discover_one(issuer: BmvIssuer, include_events: bool) -> list:
    session = _make_session()
    financial = _session_get(session, issuer.financial_url, timeout=120, verify_ssl=True).text
    records = parse_financial_page(financial, issuer)
    if include_events:
        events = _session_get(session, issuer.events_url, timeout=120, verify_ssl=True).text
        records.extend(parse_events_page(events, issuer))
    return records


def discover(issuers: list[BmvIssuer], *, include_events: bool, workers: int) -> list:
    records = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(_discover_one, issuer, include_events): issuer for issuer in issuers}
        for done, future in enumerate(as_completed(jobs), 1):
            issuer = jobs[future]
            try:
                found = future.result()
                records.extend(found)
                annuals = sum(r.doc_type == "annual_report" for r in found)
                events = sum(r.doc_type == "relevant_event" for r in found)
                print(f"[{done}/{len(jobs)}] {issuer.ticker}: annuals={annuals} events={events}")
            except Exception as exc:
                print(f"[{done}/{len(jobs)}] FAILED {issuer.ticker}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
    # A document linked in multiple sections is still one source-native record.
    return list({(r.source, r.source_record_id): r for r in records}.values())


def _relative(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _search_text_from_pdf(path: Path) -> tuple[str, int]:
    """Fast page-preserving text extraction for broad search ingestion.

    Annual reports can be hundreds of pages; the financial-model parser is intentionally more
    expensive than search needs. PyMuPDF retains page provenance and handles native-text PDFs in
    seconds. A below-threshold result is rejected for the future OCR queue rather than indexed.
    """
    import fitz

    parts: list[str] = []
    with fitz.open(path) as pdf:
        for page_num, page in enumerate(pdf, 1):
            text = page.get_text("text", sort=True).strip()
            if text:
                parts.append(f"## Page {page_num}\n\n{text}")
        pages = pdf.page_count
    return "\n\n".join(parts), pages


def _download_and_extract(record) -> tuple[Document | None, CatalogDocument]:
    def rejected(reason: str, *, original_path: str | None = None) -> CatalogDocument:
        base = CatalogDocument.from_source_record(record)
        return CatalogDocument(
            **{**base.__dict__, "status": "rejected", "rejection_reason": reason,
               "original_path": original_path}
        )

    raw_dir = RAW_DIR / record.company
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / record.source_record_id
    if not raw_path.exists():
        response = _session_get(
            _make_session(), record.canonical_url, timeout=180, verify_ssl=True
        )
        payload = response.content
        if not payload.startswith(b"%PDF"):
            return None, rejected("source is not a PDF")
        tmp = raw_path.with_suffix(raw_path.suffix + ".tmp")
        tmp.write_bytes(payload)
        os.replace(tmp, raw_path)

    derived_dir = DERIVED_DIR / record.proposed_doc_id
    derived_dir.mkdir(parents=True, exist_ok=True)
    md_path = derived_dir / "text.md"
    try:
        markdown, page_count = _search_text_from_pdf(raw_path)
    except Exception as exc:
        return None, rejected(
            f"PDF extraction failed: {type(exc).__name__}: {exc}",
            original_path=_relative(raw_path),
        )
    if len(markdown.strip()) < MIN_TEXT_CHARS:
        return None, rejected(
            f"extracted text below {MIN_TEXT_CHARS} characters",
            original_path=_relative(raw_path),
        )
    md_path.write_text(markdown, encoding="utf-8")
    digest = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    extra = {
        **record.metadata, "source": record.source,
        "source_record_id": record.source_record_id, "ticker": record.ticker,
        "published_at": record.published_at, "filed_at": record.filed_at,
        "document_family_id": record.document_family_id, "version": record.version,
        "pages": page_count,
    }
    doc = Document(
        doc_id=record.proposed_doc_id, company=record.company, period=record.period,
        doc_type=record.doc_type, title=record.title, source_url=record.canonical_url,
        pdf_path=_relative(raw_path), markdown_path=_relative(md_path), language=record.language or "es",
        industry=record.metadata.get("industry"),
        memberships=[{"company": record.company, "industry": record.metadata.get("industry")}],
        extra=extra, source_path=_relative(raw_path), source_format="pdf",
        original_path=_relative(raw_path), original_format="pdf", content_sha256=digest,
    )
    return doc, CatalogDocument.from_manifest_document(doc)


def materialize(records: list, *, workers: int) -> tuple[list[Document], int]:
    accepted: list[Document] = []
    catalog_rows: list[CatalogDocument] = []
    # Relevant events can be numerous; materialization intentionally requires the caller's
    # discovery set and applies the same extraction gate as annual reports.
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        jobs = {pool.submit(_download_and_extract, record): record for record in records}
        for done, future in enumerate(as_completed(jobs), 1):
            record = jobs[future]
            try:
                doc, catalog_row = future.result()
            except Exception as exc:
                base = CatalogDocument.from_source_record(record)
                doc, catalog_row = None, CatalogDocument(
                    **{**base.__dict__, "status": "rejected", "rejection_reason":
                       f"download failed: {type(exc).__name__}: {exc}"}
                )
            catalog_rows.append(catalog_row)
            if doc:
                accepted.append(doc)
            print(f"[{done}/{len(jobs)}] {'accepted' if doc else 'rejected'} {record.source_record_id}")

    manifest = load_manifest(CORPUS_DIR)
    by_id = {d.doc_id: d for d in manifest.documents}
    by_id.update({d.doc_id: d for d in accepted})
    save_manifest(CorpusManifest(list(by_id.values())), CORPUS_DIR)
    with CatalogStore(CATALOG_PATH) as catalog:
        catalog.upsert_many(catalog_rows)
    return accepted, len(catalog_rows) - len(accepted)


def index_accepted(documents: list[Document], index_path: Path) -> int:
    """Incrementally index accepted BMV PDFs without clearing News or prior filings.

    Materialisation deliberately writes the manifest atomically.  This companion step only runs
    after that succeeds, and uses the same replace-safe per-document operation as uploads and
    News.  An interrupted batch can therefore be resumed from the catalog without rebuilding a
    large live index or losing a separately maintained News corpus.
    """
    if not documents:
        return 0
    cfg = yaml.safe_load((ROOT / "configs" / "alpha_go.yaml").read_text(encoding="utf-8")) or {}
    store = IndexStore(index_path)
    store.connect()
    store.migrate()
    if store.get_meta("embedding_model") == "hashing":
        cfg.setdefault("index", {}).update({
            "embedding_backend": "hashing", "strict_runtime": False,
            "hashing_dim": int(store.get_meta("embedding_dim") or 256),
        })
    embedder, _name = get_embedder(cfg)
    for document in documents:
        markdown_path = Path(document.markdown_path)
        if not markdown_path.is_absolute():
            markdown_path = ROOT / markdown_path
        add_document_to_index(store, document, markdown_path.read_text(encoding="utf-8"), cfg,
                              embedder=embedder)
    store.set_meta("manifest_checksum", manifest_checksum(load_manifest(CORPUS_DIR)))
    store.commit()
    store.close()
    return len(documents)


def _records_needed_for_target(records: list, *, target: int, companies: set[str]) -> list:
    """Select the newest source-native records needed to reach a company document target.

    This makes a 50 × 50 build resumable: already materialized document IDs are never fetched
    again, and a company with rich event history does not crowd out under-covered peers.
    """
    manifest = load_manifest(CORPUS_DIR)
    existing_ids = {doc.doc_id for doc in manifest.documents}
    current: dict[str, int] = {}
    for doc in manifest.documents:
        if not companies or doc.company in companies:
            current[doc.company] = current.get(doc.company, 0) + 1
    grouped: dict[str, list] = {}
    for record in records:
        if companies and record.company not in companies:
            continue
        if record.proposed_doc_id in existing_ids:
            continue
        grouped.setdefault(record.company, []).append(record)
    selected = []
    for company, candidates in grouped.items():
        needed = max(0, target - current.get(company, 0))
        if not needed:
            continue
        # BMV records carry an ISO-style published/filing date. Descending source date gives
        # recent disclosures priority, while proposed ID makes an interrupted run deterministic.
        candidates.sort(key=lambda r: (r.published_at or r.filed_at or "", r.proposed_doc_id),
                        reverse=True)
        selected.extend(candidates[:needed])
    return selected


def _records_from_catalog(*, companies: set[str], include_events: bool) -> list:
    """Rehydrate previously discovered BMV records without rediscovering their pages.

    Discovery and materialisation have deliberately separate failure domains.  In particular, a
    long PDF download job must be able to resume from the durable catalog after a network outage
    without issuing another full sweep of issuer pages.  Only source-native PDF records which
    were not rejected by an earlier extraction attempt are returned.
    """
    allowed_types = {"annual_report"}
    if include_events:
        allowed_types.add("relevant_event")
    records = []
    with CatalogStore(CATALOG_PATH) as catalog:
        for row in catalog.iter_documents():
            if row.source != "BMV issuer" or row.doc_type not in allowed_types:
                continue
            if companies and row.company not in companies:
                continue
            if row.status == "rejected" or not row.canonical_url:
                continue
            # The BMV financial/events pages may contain links to non-document artefacts.  Keep
            # the same PDF boundary that a live discovery pass applies.
            if row.mime_type and row.mime_type != "application/pdf":
                continue
            records.append(SourceRecord(
                source=row.source, source_record_id=row.source_record_id,
                company=row.company, ticker=row.ticker, doc_type=row.doc_type,
                title=row.title, canonical_url=row.canonical_url,
                published_at=row.published_at, filed_at=row.filed_at, period=row.period,
                language=row.language, mime_type=row.mime_type,
                document_family_id=row.document_family_id, version=row.version,
                metadata=row.metadata,
            ))
    return records


def _round_robin_limit(records: list, limit: int | None) -> list:
    """Cap a broad build fairly, so high-volume issuers cannot consume the first batch."""
    if limit is None or len(records) <= limit:
        return records
    grouped: dict[str, list] = {}
    for record in records:
        grouped.setdefault(record.company, []).append(record)
    for candidates in grouped.values():
        candidates.sort(key=lambda r: (r.published_at or r.filed_at or "", r.proposed_doc_id),
                        reverse=True)
    selected: list = []
    while len(selected) < limit:
        progressed = False
        for company in sorted(grouped):
            candidates = grouped[company]
            if candidates:
                selected.append(candidates.pop(0))
                progressed = True
                if len(selected) >= limit:
                    break
        if not progressed:
            break
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-events", action="store_true")
    parser.add_argument("--include-sustainability", action="store_true",
                        help="Materialize sustainability PDFs too (discovery always catalogs them)")
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--companies", help="comma-separated company slugs; defaults to all configured issuers")
    parser.add_argument("--from-catalog", action="store_true",
                        help="resume materialisation from already-discovered BMV catalog records")
    parser.add_argument("--target-per-company", type=int,
                        help="download only the newest records needed to reach this accepted-document target")
    parser.add_argument("--limit", type=int,
                        help="maximum records this run; distributes a broad batch fairly by company")
    parser.add_argument("--index", type=Path,
                        help="incrementally update this live index with accepted PDFs")
    args = parser.parse_args(argv)

    selected = {value.strip() for value in (args.companies or "").split(",") if value.strip()}
    configured = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    configured_slugs = {str(row["slug"]) for row in configured.get("companies", [])}
    unknown = selected - configured_slugs
    if unknown:
        parser.error("unknown issuer slug(s): " + ", ".join(sorted(unknown)))
    if args.target_per_company is not None and args.target_per_company < 1:
        parser.error("--target-per-company must be positive")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if args.index and not args.download:
        parser.error("--index requires --download")
    company_scope = selected or configured_slugs
    if args.from_catalog:
        if not args.download:
            parser.error("--from-catalog is only useful together with --download")
        records = _records_from_catalog(companies=company_scope, include_events=args.include_events)
        print(f"Loaded {len(records)} eligible records from durable BMV catalog")
        issuers = []
    else:
        issuers = [issuer for issuer in load_issuers() if not selected or issuer.slug in selected]
        records = discover(issuers, include_events=args.include_events, workers=args.workers)
        with CatalogStore(CATALOG_PATH) as catalog:
            catalog.upsert_many(CatalogDocument.from_source_record(r) for r in records)
    annuals = [r for r in records if r.doc_type == "annual_report"]
    events = [r for r in records if r.doc_type == "relevant_event"]
    print(f"Discovered: annual_reports={len(annuals)} relevant_events={len(events)} total={len(records)}")
    if args.download:
        download_records = [
            r for r in records
            if r.doc_type == "annual_report"
            or (args.include_sustainability and r.doc_type == "sustainability_report")
            or (args.include_events and r.doc_type == "relevant_event")
        ]
        if args.target_per_company is not None:
            download_records = _records_needed_for_target(
                download_records, target=args.target_per_company,
                companies=company_scope,
            )
            print(f"Targeted materialization: {len(download_records)} records needed to reach "
                  f"{args.target_per_company} document(s) per selected company")
        download_records = _round_robin_limit(download_records, args.limit)
        if args.limit is not None:
            print(f"Batch cap: materializing {len(download_records)} records this run")
        accepted, rejected = materialize(download_records, workers=args.workers)
        print(f"Materialized: accepted={len(accepted)} rejected={rejected}")
        if args.index:
            index_path = args.index if args.index.is_absolute() else ROOT / args.index
            print(f"Indexed accepted PDFs: {index_accepted(accepted, index_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
