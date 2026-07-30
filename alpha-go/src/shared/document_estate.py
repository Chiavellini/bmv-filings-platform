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

    @staticmethod
    def _artifact_path_keys(
        path: Path,
        *,
        portable_root: str | Path | None = None,
    ) -> tuple[str, ...]:
        """Return absolute and, when requested, bundle-relative catalog keys."""

        resolved = path.expanduser().resolve()
        keys = [str(resolved)]
        if portable_root is not None:
            root = Path(portable_root).expanduser().resolve()
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                pass
            else:
                keys.insert(0, relative.as_posix())
        return tuple(dict.fromkeys(keys))

    def artifact_document(
        self,
        path: Path,
        *,
        portable_root: str | Path | None = None,
    ) -> str | None:
        """Return the logical document already owning ``path``, if catalogued."""
        if portable_root is None:
            portable_root = self.path.parent
        keys = self._artifact_path_keys(path, portable_root=portable_root)
        placeholders = ",".join("?" for _ in keys)
        row = self.conn.execute(
            f"SELECT document_id FROM artifacts WHERE path IN ({placeholders}) "
            "ORDER BY CASE WHEN path=? THEN 0 ELSE 1 END LIMIT 1",
            (*keys, keys[0]),
        ).fetchone()
        return row["document_id"] if row else None

    def artifact_id(
        self,
        path: Path,
        *,
        portable_root: str | Path | None = None,
    ) -> str | None:
        """Return the artifact id for either an absolute or portable path."""

        if portable_root is None:
            portable_root = self.path.parent
        keys = self._artifact_path_keys(path, portable_root=portable_root)
        placeholders = ",".join("?" for _ in keys)
        row = self.conn.execute(
            f"SELECT artifact_id FROM artifacts WHERE path IN ({placeholders}) "
            "ORDER BY CASE WHEN path=? THEN 0 ELSE 1 END LIMIT 1",
            (*keys, keys[0]),
        ).fetchone()
        return row["artifact_id"] if row else None

    def add_project_record(self, project: str, record_id: str, document_id: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO project_records(project,project_record_id,document_id) VALUES(?,?,?)",
            (project, record_id, document_id),
        )

    def add_artifact(
        self,
        document_id: str,
        path: Path,
        *,
        project: str,
        role: str,
        portable_root: str | Path | None = None,
    ) -> str:
        resolved = path.resolve()
        sha, size, mtime = self.hash_file(resolved)
        keys = self._artifact_path_keys(resolved, portable_root=portable_root)
        placeholders = ",".join("?" for _ in keys)
        existing = self.conn.execute(
            f"SELECT path FROM artifacts WHERE path IN ({placeholders}) LIMIT 1",
            keys,
        ).fetchone()
        stored_path = existing["path"] if existing else keys[0]
        artifact_id = hashlib.sha256(
            f"{document_id}\0{stored_path}".encode()
        ).hexdigest()
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
              stored_path, sha, size, mtime, resolved.stat().st_dev, resolved.stat().st_ino,
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


# --------------------------------------------------------------------------- #
# Read-side API. Subprojects consume the estate through EstateReader, which
# opens the catalog with SQLite's ?mode=ro so it physically cannot violate the
# estate's read-only contract (docs/DOCUMENT_ESTATE.md).
# --------------------------------------------------------------------------- #

_FACTS_PERIOD_RE = None  # compiled lazily; keeps re import local to first use


@dataclass(frozen=True)
class ArtifactRef:
    document_id: str
    company: str
    period: str | None
    doc_type: str
    project: str
    role: str
    format: str
    path: Path
    sha256: str


class EstateReader:
    """Read-only estate access for consumers (earnings/, soft/, alpha-go/...).

    All lookups go through catalog.db; ``view_dir`` exposes the symlink view for
    glob-style consumers. Company names are resolved through
    ``configs/company_aliases.yaml`` plus the catalog's memberships table.
    """

    _QUERY = """
        SELECT a.document_id, d.company, d.period, d.doc_type,
               a.project, a.role, a.format, a.path, a.sha256
        FROM artifacts a JOIN documents d USING (document_id)
    """

    def __init__(self, db_path: str | Path | None = None):
        if db_path is None:
            from src.shared.paths import DOCUMENT_ESTATE_DB

            db_path = DOCUMENT_ESTATE_DB
        self.path = Path(db_path)
        if not self.path.exists():
            raise FileNotFoundError(f"estate catalog not found: {self.path}")
        self.conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        self.conn.row_factory = sqlite3.Row
        self._aliases: dict[str, str] | None = None

    def _artifact_path(self, raw: str) -> Path:
        path = Path(raw)
        return path if path.is_absolute() else self.path.parent / path

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- company resolution -------------------------------------------------- #

    def _alias_map(self) -> dict[str, str]:
        if self._aliases is None:
            aliases: dict[str, str] = {}
            try:
                import yaml

                from src.shared.paths import CONFIGS_DIR

                raw = yaml.safe_load((CONFIGS_DIR / "company_aliases.yaml").read_text())
                for slug, names in (raw.get("canonical") or {}).items():
                    aliases[slug.lower()] = slug
                    for name in names or ():
                        aliases[str(name).lower()] = slug
            except FileNotFoundError:
                pass
            self._aliases = aliases
        return self._aliases

    def companies(self) -> list[str]:
        rows = self.conn.execute(
            "SELECT DISTINCT company FROM documents ORDER BY company"
        ).fetchall()
        return [r["company"] for r in rows]

    def document(self, document_id: str) -> EstateDocument | None:
        """Return one logical document, or ``None`` when it is not catalogued."""
        row = self.conn.execute(
            """SELECT document_id,company,period,doc_type,title,language,source_url,
                      published_at,metadata_json
               FROM documents WHERE document_id=?""",
            (document_id,),
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(row["metadata_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return EstateDocument(
            document_id=row["document_id"],
            company=row["company"],
            period=row["period"],
            doc_type=row["doc_type"],
            title=row["title"],
            language=row["language"],
            source_url=row["source_url"],
            published_at=row["published_at"],
            metadata=metadata,
        )

    def artifacts_for_document(
        self,
        document_id: str,
        *,
        role: str | None = None,
        fmt: str | None = None,
    ) -> list[ArtifactRef]:
        """Return artifacts belonging to one document with optional exact filters."""
        clauses = ["a.document_id=?"]
        params: list[str] = [document_id]
        if role is not None:
            clauses.append("a.role=?")
            params.append(role)
        if fmt is not None:
            clauses.append("a.format=?")
            params.append(fmt)
        sql = self._QUERY + " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY a.project, a.role, a.format, a.path"
        return [
            ArtifactRef(
                document_id=row["document_id"],
                company=row["company"],
                period=row["period"],
                doc_type=row["doc_type"],
                project=row["project"],
                role=row["role"],
                format=row["format"],
                path=self._artifact_path(row["path"]),
                sha256=row["sha256"],
            )
            for row in self.conn.execute(sql, params)
        ]

    def projects_for_document(self, document_id: str) -> list[str]:
        """Return the document's intended consumer projects.

        Acquisition pins in ``document_projects`` are authoritative. Catalogs
        created before that table existed fall back to project records and
        artifact ownership, preserving the read API for legacy estate rows.
        """
        has_pins = self.conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='document_projects'"""
        ).fetchone()
        if has_pins:
            rows = self.conn.execute(
                """SELECT project FROM document_projects
                   WHERE document_id=? ORDER BY project""",
                (document_id,),
            ).fetchall()
            if rows:
                return [row["project"] for row in rows]

        rows = self.conn.execute(
            """SELECT project FROM project_records WHERE document_id=?
               UNION
               SELECT project FROM artifacts WHERE document_id=?
               ORDER BY project""",
            (document_id, document_id),
        ).fetchall()
        return [row["project"] for row in rows]

    def resolve_company(self, name: str) -> str:
        """Canonical catalog company for ``name`` (slug, alias, or membership)."""
        candidate = name.strip().lower()
        exists = self.conn.execute(
            "SELECT 1 FROM documents WHERE company=? LIMIT 1", (candidate,)
        ).fetchone()
        if exists:
            return candidate
        canonical = self._alias_map().get(candidate)
        if canonical and self.conn.execute(
            "SELECT 1 FROM documents WHERE company=? LIMIT 1", (canonical,)
        ).fetchone():
            return canonical
        member = self.conn.execute(
            """SELECT d.company FROM memberships m JOIN documents d USING (document_id)
               WHERE m.company=? LIMIT 1""",
            (candidate,),
        ).fetchone()
        if member:
            return member["company"]
        return candidate

    # -- artifact lookups ---------------------------------------------------- #

    def artifacts(
        self,
        company: str | None = None,
        *,
        period: str | None = None,
        doc_type: str | None = None,
        project: str | None = None,
        role: str | None = None,
        fmt: str | None = None,
        path_suffix: str | None = None,
    ) -> list[ArtifactRef]:
        clauses, params = [], []
        if company is not None:
            clauses.append("d.company=?")
            params.append(self.resolve_company(company))
        for column, value in (
            ("d.period", period),
            ("d.doc_type", doc_type),
            ("a.project", project),
            ("a.role", role),
            ("a.format", fmt),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                params.append(value)
        if path_suffix is not None:
            clauses.append("a.path LIKE ?")
            params.append(f"%{path_suffix}")
        sql = self._QUERY
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY d.company, d.period, a.path"
        return [
            ArtifactRef(
                document_id=r["document_id"],
                company=r["company"],
                period=r["period"],
                doc_type=r["doc_type"],
                project=r["project"],
                role=r["role"],
                format=r["format"],
                path=self._artifact_path(r["path"]),
                sha256=r["sha256"],
            )
            for r in self.conn.execute(sql, params)
        ]

    def xbrl_facts_map(
        self,
        company: str,
        *,
        project: str = "soft",
        role: str | None = None,
    ) -> dict[str, Path]:
        """Return the current ``period -> facts path`` map for one project.

        ``role`` is optional for legacy compatibility. Canonical root consumers
        should pass ``project="root", role="xbrl_facts"`` so similarly named
        JSON produced by another root workflow cannot enter the result.

        When acquisition version metadata exists for a company/period, only
        artifacts owned by the latest document in each source-record family are
        eligible. This deliberately fails closed while a corrected filing is
        waiting for its facts derivative instead of silently returning the
        superseded version. Catalogs and periods without
        ``source_record_versions`` retain the historical filename-based
        behavior.
        """
        global _FACTS_PERIOD_RE
        if _FACTS_PERIOD_RE is None:
            import re

            _FACTS_PERIOD_RE = re.compile(r"_(\d{4}-(?:[1-4]T|FY))_facts$")
        from src.shared.report_index import infer_period_label

        resolved_company = self.resolve_company(company)
        current_by_period: dict[str, dict[str, tuple[int, str]]] = {}
        has_versions = self.conn.execute(
            """SELECT 1 FROM sqlite_master
               WHERE type='table' AND name='source_record_versions'"""
        ).fetchone()
        if has_versions:
            for row in self.conn.execute(
                """SELECT d.period,v.document_id,v.document_family_id,
                          v.version,v.stored_at
                   FROM source_record_versions v
                   JOIN documents d ON d.document_id=v.document_id
                   JOIN (
                       SELECT document_family_id,MAX(version) AS version
                       FROM source_record_versions
                       GROUP BY document_family_id
                   ) latest
                     ON latest.document_family_id=v.document_family_id
                    AND latest.version=v.version
                   WHERE d.company=? AND d.period IS NOT NULL""",
                (resolved_company,),
            ):
                current_by_period.setdefault(row["period"], {})[
                    row["document_id"]
                ] = (int(row["version"]), str(row["stored_at"]))

        selected: dict[str, tuple[tuple[object, ...], Path]] = {}
        for ref in self.artifacts(
            resolved_company,
            project=project,
            role=role,
            fmt="json",
            path_suffix="_facts.json",
        ):
            stem = ref.path.name[: -len(".json")]
            match = _FACTS_PERIOD_RE.search(stem)
            period = match.group(1) if match else (ref.period or infer_period_label(stem))
            if period is None:
                continue
            current_documents = current_by_period.get(period)
            if current_documents:
                current = current_documents.get(ref.document_id)
                if current is None:
                    # A current acquired filing exists for this period. Never
                    # fall back to a superseded or unversioned facts sidecar.
                    continue
                version, stored_at = current
                score: tuple[object, ...] = (
                    1,
                    stored_at,
                    version,
                    -len(ref.path.name),
                    ref.path.as_posix(),
                )
            else:
                # Preserve catalogs that predate the acquisition ledger.
                score = (
                    0,
                    "",
                    0,
                    -len(ref.path.name),
                    ref.path.as_posix(),
                )
            previous = selected.get(period)
            if previous is None or score > previous[0]:
                selected[period] = (score, ref.path)
        return {
            period: selected[period][1]
            for period in sorted(selected)
        }

    def view_dir(self, company: str) -> Path | None:
        """``SHARED_REPORTS_DIR/<company>`` for glob-style consumers, if present."""
        from src.shared.paths import SHARED_REPORTS_DIR

        candidate = SHARED_REPORTS_DIR / self.resolve_company(company)
        return candidate if candidate.is_dir() else None
