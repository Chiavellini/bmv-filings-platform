"""SQLite document catalog independent from the search index.

The manifest remains the compatibility hand-off to the existing indexer.  This catalog is the
durable acquisition ledger: it records source identity, dates, versions, local originals and the
processing state for both downloaded and uploaded documents.  It is safe to rebuild from the
manifest, but unlike the manifest it can also retain discovered/rejected records that never enter
search.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from src.corpus.manifest import CorpusManifest, Document

_STATUSES = frozenset({"discovered", "downloaded", "extracted", "indexed", "rejected"})


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _fiscal_fields(period: str | None) -> tuple[int | None, int | None]:
    if not period:
        return None, None
    try:
        year, suffix = period.split("-", 1)
        fiscal_year = int(year)
    except (TypeError, ValueError):
        return None, None
    if suffix.endswith("T"):
        try:
            return fiscal_year, int(suffix[:-1])
        except ValueError:
            return fiscal_year, None
    return fiscal_year, None


@dataclass(frozen=True)
class CatalogDocument:
    doc_id: str
    company: str
    doc_type: str
    title: str
    source: str
    source_record_id: str
    status: str = "discovered"
    ticker: str | None = None
    canonical_url: str | None = None
    published_at: str | None = None
    filed_at: str | None = None
    period: str | None = None
    fiscal_year: int | None = None
    fiscal_quarter: int | None = None
    period_start: str | None = None
    period_end: str | None = None
    language: str | None = None
    mime_type: str | None = None
    original_path: str | None = None
    markdown_path: str | None = None
    content_sha256: str | None = None
    document_family_id: str | None = None
    version: int = 1
    supersedes_doc_id: str | None = None
    rejection_reason: str | None = None
    memberships: tuple[dict, ...] = ()
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.doc_id.strip():
            raise ValueError("doc_id is required")
        if not self.source.strip() or not self.source_record_id.strip():
            raise ValueError("source and source_record_id are required")
        if self.status not in _STATUSES:
            raise ValueError(f"Unknown catalog status: {self.status!r}")
        if self.status == "rejected" and not self.rejection_reason:
            raise ValueError("rejected records require rejection_reason")

    @classmethod
    def from_manifest_document(
        cls, doc: Document, *, indexed: bool = False,
    ) -> "CatalogDocument":
        extra = doc.extra or {}
        source = str(extra.get("source") or ("upload" if extra.get("uploaded") else "manifest"))
        source_record_id = str(
            # IR landing pages are shared by many reports and are therefore not record IDs.
            # Only use a source-native ID/accession when one was explicitly captured; the stable
            # legacy doc_id is the safe migration identity for every other manifest record.
            extra.get("source_record_id") or extra.get("accession") or doc.doc_id
        )
        fiscal_year, fiscal_quarter = _fiscal_fields(doc.period)
        original_format = (doc.original_format or doc.source_format or "").lower()
        mime = {
            "pdf": "application/pdf", "html": "text/html", "htm": "text/html",
            "md": "text/markdown", "txt": "text/plain",
        }.get(original_format)
        memberships = tuple(doc.memberships or (
            {"company": doc.company, "industry": doc.industry},
        ))
        return cls(
            doc_id=doc.doc_id, company=doc.company, doc_type=doc.doc_type, title=doc.title,
            source=source, source_record_id=source_record_id,
            status="indexed" if indexed else "extracted", ticker=extra.get("ticker"),
            canonical_url=doc.source_url, published_at=extra.get("published_at"),
            filed_at=extra.get("filed_at") or extra.get("filed_date"), period=doc.period,
            fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter,
            period_start=extra.get("period_start"), period_end=extra.get("period_end"),
            language=doc.language, mime_type=mime, original_path=doc.original_path,
            markdown_path=doc.markdown_path, content_sha256=doc.content_sha256,
            document_family_id=extra.get("document_family_id") or doc.doc_id,
            version=int(extra.get("version") or 1),
            supersedes_doc_id=extra.get("supersedes_doc_id"), memberships=memberships,
            metadata=extra,
        )

    @classmethod
    def from_source_record(cls, record, *, status: str = "discovered") -> "CatalogDocument":
        """Create a pre-download catalog row from any ``SourceRecord``-compatible object."""
        fiscal_year, fiscal_quarter = _fiscal_fields(record.period)
        industry = (record.metadata or {}).get("industry")
        return cls(
            doc_id=record.proposed_doc_id, company=record.company, ticker=record.ticker,
            doc_type=record.doc_type, title=record.title, source=record.source,
            source_record_id=record.source_record_id, canonical_url=record.canonical_url,
            published_at=record.published_at, filed_at=record.filed_at, period=record.period,
            fiscal_year=fiscal_year, fiscal_quarter=fiscal_quarter,
            language=record.language, mime_type=record.mime_type,
            document_family_id=record.document_family_id, version=record.version, status=status,
            memberships=({"company": record.company, "industry": industry},),
            metadata=record.metadata or {},
        )


@dataclass(frozen=True)
class CatalogStats:
    documents: int
    memberships: int
    by_status: dict[str, int]
    by_doc_type: dict[str, int]


class CatalogStore:
    """Transactional catalog store; callers own no raw sqlite connection state."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self.migrate()

    def __enter__(self) -> "CatalogStore":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        self._conn.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS catalog_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS documents (
                doc_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                ticker TEXT,
                doc_type TEXT NOT NULL,
                title TEXT NOT NULL,
                source TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                canonical_url TEXT,
                published_at TEXT,
                filed_at TEXT,
                period TEXT,
                fiscal_year INTEGER,
                fiscal_quarter INTEGER,
                period_start TEXT,
                period_end TEXT,
                language TEXT,
                mime_type TEXT,
                original_path TEXT,
                markdown_path TEXT,
                content_sha256 TEXT,
                document_family_id TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                supersedes_doc_id TEXT,
                status TEXT NOT NULL,
                rejection_reason TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                discovered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source, source_record_id),
                FOREIGN KEY(supersedes_doc_id) REFERENCES documents(doc_id)
            );
            CREATE TABLE IF NOT EXISTS document_memberships (
                doc_id TEXT NOT NULL,
                company TEXT NOT NULL,
                industry TEXT,
                PRIMARY KEY(doc_id, company),
                FOREIGN KEY(doc_id) REFERENCES documents(doc_id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_catalog_company ON documents(company);
            CREATE INDEX IF NOT EXISTS idx_catalog_type_period
                ON documents(doc_type, fiscal_year, fiscal_quarter);
            CREATE INDEX IF NOT EXISTS idx_catalog_status ON documents(status);
            CREATE INDEX IF NOT EXISTS idx_catalog_membership_company
                ON document_memberships(company);
            """
        )
        self._conn.execute(
            "INSERT OR REPLACE INTO catalog_meta(key, value) VALUES('schema_version', '1')"
        )
        self._conn.commit()

    def upsert(self, record: CatalogDocument) -> None:
        now = _utc_now()
        values = (
            record.doc_id, record.company, record.ticker, record.doc_type, record.title,
            record.source, record.source_record_id, record.canonical_url, record.published_at,
            record.filed_at, record.period, record.fiscal_year, record.fiscal_quarter,
            record.period_start, record.period_end, record.language, record.mime_type,
            record.original_path, record.markdown_path, record.content_sha256,
            record.document_family_id, record.version, record.supersedes_doc_id, record.status,
            record.rejection_reason,
            json.dumps(record.metadata, ensure_ascii=False, sort_keys=True), now, now,
        )
        self._conn.execute(
            """
            INSERT INTO documents(
                doc_id, company, ticker, doc_type, title, source, source_record_id,
                canonical_url, published_at, filed_at, period, fiscal_year, fiscal_quarter,
                period_start, period_end, language, mime_type, original_path, markdown_path,
                content_sha256, document_family_id, version, supersedes_doc_id, status,
                rejection_reason, metadata_json, discovered_at, updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(doc_id) DO UPDATE SET
                company=excluded.company, ticker=excluded.ticker, doc_type=excluded.doc_type,
                title=excluded.title, source=excluded.source,
                source_record_id=excluded.source_record_id,
                canonical_url=excluded.canonical_url, published_at=excluded.published_at,
                filed_at=excluded.filed_at, period=excluded.period,
                fiscal_year=excluded.fiscal_year, fiscal_quarter=excluded.fiscal_quarter,
                period_start=excluded.period_start, period_end=excluded.period_end,
                language=excluded.language, mime_type=excluded.mime_type,
                original_path=excluded.original_path, markdown_path=excluded.markdown_path,
                content_sha256=excluded.content_sha256,
                document_family_id=excluded.document_family_id, version=excluded.version,
                supersedes_doc_id=excluded.supersedes_doc_id, status=excluded.status,
                rejection_reason=excluded.rejection_reason,
                metadata_json=excluded.metadata_json, updated_at=excluded.updated_at
            """, values,
        )
        self._conn.execute("DELETE FROM document_memberships WHERE doc_id = ?", (record.doc_id,))
        memberships = record.memberships or ({"company": record.company, "industry": None},)
        self._conn.executemany(
            "INSERT INTO document_memberships(doc_id, company, industry) VALUES(?,?,?)",
            [(record.doc_id, m["company"], m.get("industry")) for m in memberships],
        )

    def upsert_many(self, records: Iterable[CatalogDocument]) -> int:
        count = 0
        with self._conn:
            for record in records:
                self.upsert(record)
                count += 1
        return count

    def reconcile_manifest(
        self, manifest: CorpusManifest, *, indexed_doc_ids: set[str] | None = None,
    ) -> int:
        # A manifest-only reconciliation must never demote records that a prior index-aware sync
        # already certified. New extracted records remain extracted until index promotion.
        indexed_doc_ids = set(indexed_doc_ids or ())
        indexed_doc_ids.update(
            row[0] for row in self._conn.execute(
                "SELECT doc_id FROM documents WHERE status='indexed'"
            )
        )
        records = (
            CatalogDocument.from_manifest_document(d, indexed=d.doc_id in indexed_doc_ids)
            for d in manifest.documents
        )
        count = self.upsert_many(records)
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO catalog_meta(key, value) VALUES('last_manifest_sync', ?)",
                (_utc_now(),),
            )
        return count

    def get(self, doc_id: str) -> CatalogDocument | None:
        row = self._conn.execute("SELECT * FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        return self._from_row(row) if row else None

    def iter_documents(self, *, company: str | None = None) -> Iterator[CatalogDocument]:
        if company:
            rows = self._conn.execute(
                """SELECT DISTINCT d.* FROM documents d
                   JOIN document_memberships m ON m.doc_id=d.doc_id
                   WHERE m.company=? ORDER BY d.doc_id""", (company,),
            )
        else:
            rows = self._conn.execute("SELECT * FROM documents ORDER BY doc_id")
        for row in rows:
            yield self._from_row(row)

    def stats(self) -> CatalogStats:
        def grouped(column: str) -> dict[str, int]:
            return {r[0]: r[1] for r in self._conn.execute(
                f"SELECT {column}, COUNT(*) FROM documents GROUP BY {column}"  # noqa: S608
            )}
        return CatalogStats(
            documents=self._conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0],
            memberships=self._conn.execute(
                "SELECT COUNT(*) FROM document_memberships"
            ).fetchone()[0],
            by_status=grouped("status"), by_doc_type=grouped("doc_type"),
        )

    def _from_row(self, row: sqlite3.Row) -> CatalogDocument:
        memberships = tuple(dict(r) for r in self._conn.execute(
            "SELECT company, industry FROM document_memberships WHERE doc_id=? ORDER BY rowid",
            (row["doc_id"],),
        ))
        return CatalogDocument(
            doc_id=row["doc_id"], company=row["company"], ticker=row["ticker"],
            doc_type=row["doc_type"], title=row["title"], source=row["source"],
            source_record_id=row["source_record_id"], canonical_url=row["canonical_url"],
            published_at=row["published_at"], filed_at=row["filed_at"], period=row["period"],
            fiscal_year=row["fiscal_year"], fiscal_quarter=row["fiscal_quarter"],
            period_start=row["period_start"], period_end=row["period_end"],
            language=row["language"], mime_type=row["mime_type"],
            original_path=row["original_path"], markdown_path=row["markdown_path"],
            content_sha256=row["content_sha256"],
            document_family_id=row["document_family_id"], version=row["version"],
            supersedes_doc_id=row["supersedes_doc_id"], status=row["status"],
            rejection_reason=row["rejection_reason"], memberships=memberships,
            metadata=json.loads(row["metadata_json"] or "{}"),
        )
