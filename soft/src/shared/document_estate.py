"""Shared, project-neutral document estate for the pdfs/ monorepo.

The estate is a metadata catalog. Original files remain where they are until an explicit,
verified storage migration is undertaken; every artifact is addressed by an absolute path and
content hash, so Alpha Go, the root extraction stack, and soft can safely share it meanwhile.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True)
class EstateDocument:
    document_id: str
    company: str
    period: str | None
    doc_type: str
    title: str
    language: str | None = None
    source_url: str | None = None
    published_at: str | None = None
    metadata: dict | None = None


class DocumentEstate:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.migrate()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        self.conn.close()

    def migrate(self) -> None:
        self.conn.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS documents (
                document_id TEXT PRIMARY KEY,
                company TEXT NOT NULL,
                period TEXT,
                doc_type TEXT NOT NULL,
                title TEXT NOT NULL,
                language TEXT,
                source_url TEXT,
                published_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS artifacts (
                artifact_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                project TEXT NOT NULL,
                role TEXT NOT NULL,
                format TEXT NOT NULL,
                path TEXT NOT NULL UNIQUE,
                sha256 TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                st_dev INTEGER,
                st_ino INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS project_records (
                project TEXT NOT NULL,
                project_record_id TEXT NOT NULL,
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                PRIMARY KEY(project, project_record_id)
            );
            CREATE TABLE IF NOT EXISTS memberships (
                document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                company TEXT NOT NULL,
                industry TEXT,
                PRIMARY KEY(document_id, company)
            );
            CREATE TABLE IF NOT EXISTS hash_cache (
                path TEXT PRIMARY KEY,
                size_bytes INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS content_objects (
                sha256 TEXT PRIMARY KEY,
                blob_path TEXT NOT NULL UNIQUE,
                size_bytes INTEGER NOT NULL,
                st_dev INTEGER NOT NULL,
                st_ino INTEGER NOT NULL,
                verified_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS consolidation_runs (
                run_id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                planned_actions INTEGER NOT NULL DEFAULT 0,
                completed_actions INTEGER NOT NULL DEFAULT 0,
                reclaimed_bytes INTEGER NOT NULL DEFAULT 0,
                journal_path TEXT NOT NULL,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS consolidation_actions (
                run_id TEXT NOT NULL REFERENCES consolidation_runs(run_id),
                ordinal INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                artifact_path TEXT NOT NULL,
                blob_path TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                old_dev INTEGER NOT NULL,
                old_inode INTEGER NOT NULL,
                old_nlink INTEGER NOT NULL,
                old_mode INTEGER NOT NULL,
                old_mtime_ns INTEGER NOT NULL,
                status TEXT NOT NULL,
                error TEXT,
                PRIMARY KEY(run_id, ordinal)
            );
            CREATE INDEX IF NOT EXISTS idx_estate_company_period
                ON documents(company, period, doc_type);
            CREATE INDEX IF NOT EXISTS idx_estate_artifact_hash ON artifacts(sha256);
            CREATE INDEX IF NOT EXISTS idx_estate_project ON project_records(project);
            CREATE VIEW IF NOT EXISTS artifact_duplicates AS
                SELECT sha256, COUNT(*) AS copies, SUM(size_bytes) AS logical_bytes
                FROM artifacts GROUP BY sha256 HAVING COUNT(*) > 1;
        """)
        # Add inode identity to catalogs created before physical consolidation support.
        artifact_columns = {
            row["name"] for row in self.conn.execute("PRAGMA table_info(artifacts)")
        }
        for column in ("st_dev", "st_ino"):
            if column not in artifact_columns:
                self.conn.execute(f"ALTER TABLE artifacts ADD COLUMN {column} INTEGER")
        self.conn.executescript("""
            DROP VIEW IF EXISTS artifact_physical_waste;
            CREATE VIEW artifact_physical_waste AS
                SELECT sha256, paths, inodes, size_bytes,
                       (inodes - 1) * size_bytes AS wasted_bytes
                FROM (
                    SELECT sha256, COUNT(*) AS paths,
                           COUNT(DISTINCT printf('%lld:%lld', st_dev, st_ino)) AS inodes,
                           MAX(size_bytes) AS size_bytes
                    FROM artifacts
                    WHERE st_dev IS NOT NULL AND st_ino IS NOT NULL
                    GROUP BY sha256
                )
                WHERE inodes > 1;
        """)
        self.conn.commit()

    def hash_file(self, path: Path) -> tuple[str, int, int]:
        resolved = path.resolve()
        stat = resolved.stat()
        row = self.conn.execute(
            "SELECT sha256 FROM hash_cache WHERE path=? AND size_bytes=? AND mtime_ns=?",
            (str(resolved), stat.st_size, stat.st_mtime_ns),
        ).fetchone()
        sha = row["sha256"] if row else file_sha256(resolved)
        self.conn.execute(
            "INSERT OR REPLACE INTO hash_cache(path,size_bytes,mtime_ns,sha256) VALUES(?,?,?,?)",
            (str(resolved), stat.st_size, stat.st_mtime_ns, sha),
        )
        return sha, stat.st_size, stat.st_mtime_ns

    def upsert_document(self, doc: EstateDocument, memberships: Iterable[dict] = ()) -> None:
        now = _now()
        self.conn.execute("""
            INSERT INTO documents(document_id,company,period,doc_type,title,language,source_url,
                                  published_at,metadata_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(document_id) DO UPDATE SET
                company=excluded.company, period=excluded.period, doc_type=excluded.doc_type,
                title=excluded.title, language=excluded.language, source_url=excluded.source_url,
                published_at=excluded.published_at, metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
        """, (doc.document_id, doc.company, doc.period, doc.doc_type, doc.title, doc.language,
              doc.source_url, doc.published_at,
              json.dumps(doc.metadata or {}, ensure_ascii=False, sort_keys=True), now, now))
        self.add_memberships(
            doc.document_id,
            list(memberships) or [{"company": doc.company, "industry": None}],
            fallback_company=doc.company,
        )

    def add_memberships(
        self,
        document_id: str,
        memberships: Iterable[dict],
        *,
        fallback_company: str,
    ) -> None:
        """Attach company facets without replacing the document's canonical metadata."""
        rows = list(memberships) or [{"company": fallback_company, "industry": None}]
        for row in rows:
            self.conn.execute(
                "INSERT OR REPLACE INTO memberships(document_id,company,industry) VALUES(?,?,?)",
                (document_id, row.get("company") or fallback_company, row.get("industry")),
            )

    def reset_project(self, project: str) -> None:
        """Remove one project's generated catalog rows before a complete refresh.

        Source files and hash-cache rows are untouched. Documents still referenced by another
        project remain, which is what makes shared physical artifacts safe.
        """
        self.conn.execute("DELETE FROM project_records WHERE project=?", (project,))
        self.conn.execute("DELETE FROM artifacts WHERE project=?", (project,))
        self.conn.execute("""
            DELETE FROM documents
            WHERE document_id NOT IN (SELECT document_id FROM project_records)
              AND document_id NOT IN (SELECT document_id FROM artifacts)
        """)
        self.conn.commit()

    def artifact_document(self, path: Path) -> str | None:
        """Return the logical document already owning ``path``, if catalogued."""
        row = self.conn.execute(
            "SELECT document_id FROM artifacts WHERE path=?", (str(path.resolve()),)
        ).fetchone()
        return row["document_id"] if row else None

    def add_project_record(self, project: str, record_id: str, document_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO project_records(project,project_record_id,document_id) VALUES(?,?,?)",
            (project, record_id, document_id),
        )

    def add_artifact(self, document_id: str, path: Path, *, project: str, role: str) -> str:
        resolved = path.resolve()
        sha, size, mtime = self.hash_file(resolved)
        artifact_id = hashlib.sha256(f"{document_id}\0{resolved}".encode()).hexdigest()
        self.conn.execute("""
            INSERT INTO artifacts(artifact_id,document_id,project,role,format,path,sha256,
                                  size_bytes,mtime_ns,st_dev,st_ino,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            -- One physical artifact can be referenced by several project records. The first
            -- catalogued document remains its owner; a refresh must not silently transfer it.
            ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256,
                size_bytes=excluded.size_bytes,mtime_ns=excluded.mtime_ns,
                st_dev=excluded.st_dev,st_ino=excluded.st_ino
        """, (artifact_id, document_id, project, role, resolved.suffix.lower().lstrip(".") or "file",
              str(resolved), sha, size, mtime, resolved.stat().st_dev, resolved.stat().st_ino,
              _now()))
        return sha

    def commit(self) -> None:
        self.conn.commit()

    def stats(self) -> dict:
        one = self.conn.execute
        return {
            "documents": one("SELECT COUNT(*) FROM documents").fetchone()[0],
            "artifacts": one("SELECT COUNT(*) FROM artifacts").fetchone()[0],
            "companies": one("SELECT COUNT(DISTINCT company) FROM memberships").fetchone()[0],
            "projects": one("SELECT COUNT(DISTINCT project) FROM project_records").fetchone()[0],
            "bytes": one("SELECT COALESCE(SUM(size_bytes),0) FROM artifacts").fetchone()[0],
            "duplicate_hashes": one("SELECT COUNT(*) FROM artifact_duplicates").fetchone()[0],
        }
