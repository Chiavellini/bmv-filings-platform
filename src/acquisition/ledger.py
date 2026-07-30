"""Durable acquisition state stored beside the shared document estate.

The ledger deliberately shares the estate's SQLite connection.  Discovery,
attempt, document-version, and outbox rows can therefore be committed in the
same transaction as ``DocumentEstate`` documents and artifacts.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


RECORD_STATUSES = frozenset({"discovered", "retryable", "rejected", "stored"})
RUN_STATUSES = frozenset({"running", "succeeded", "partial", "failed"})

# The acquisition ledger owns the ``outbox`` table: acquisition publishes events,
# consumers only read and mark them.  Consumer modules that need the table to
# exist standalone execute this same statement rather than declaring their own
# copy — two divergent CREATE TABLE definitions for one table in one database is
# how schemas silently fork.  See docs/DOCUMENT_ESTATE.md for the ownership map.
OUTBOX_DDL = """
            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                aggregate_id TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                available_at TEXT NOT NULL,
                published_at TEXT,
                publish_attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_pending
                ON outbox(published_at, available_at);
"""


class AcquisitionLeaseHeld(RuntimeError):
    """Raised when another non-expired fleet refresh owns the singleton lease."""


class AcquisitionLeaseLost(RuntimeError):
    """Raised when a run can no longer renew its fenced acquisition lease."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _as_iso(value: object | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat(timespec="seconds")
    return str(value)


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        dict(value or {}),
        ensure_ascii=False,
        sort_keys=True,
        default=lambda item: _as_iso(item),
    )


def _record_values(record: object) -> dict[str, Any]:
    """Normalize the narrow ``SourceRecord`` protocol without importing it."""

    raw_source_key = str(getattr(record, "source_key")).strip()
    source_record_id = str(getattr(record, "source_record_id")).strip()
    company = str(
        getattr(record, "company", None) or getattr(record, "issuer_slug")
    ).strip()
    document_type = str(
        getattr(record, "doc_type", None)
        or getattr(record, "document_type", "quarterly_release")
    ).strip()
    # Registry source keys are issuer-local (many issuers legitimately use
    # ``ir`` or ``bmv-xbrl``). Qualifying the binding makes the durable
    # (source_key, source_record_id) identity globally safe.
    source_key = (
        raw_source_key
        if raw_source_key.startswith(f"{company}:")
        else f"{company}:{raw_source_key}"
    )
    period = getattr(record, "period", None)
    family = getattr(record, "document_family_id", None)
    if not family:
        family = (
            f"{company}:{document_type}:{period}"
            if period
            else f"{company}:{document_type}:{raw_source_key}:{source_record_id}"
        )
    if not raw_source_key or not source_record_id or not company or not document_type:
        raise ValueError("source_key, source_record_id, company, and document_type are required")
    return {
        "source_key": source_key,
        "source_record_id": source_record_id,
        "company": company,
        "document_type": document_type,
        "document_family_id": str(family),
        "title": getattr(record, "title", None),
        "canonical_url": getattr(record, "url", None)
        or getattr(record, "canonical_url", None),
        "period": period,
        "language": getattr(record, "language", None),
        "published_at": _as_iso(getattr(record, "published_at", None)),
        "metadata_json": _json(getattr(record, "metadata", None)),
    }


class AcquisitionLedger:
    """Acquisition run and source state over an estate SQLite database.

    Passing a path gives the ledger its own connection and makes public
    mutations durable immediately.  Passing an existing connection disables
    auto-commit so an ``EstateWriter`` can include ledger rows in its larger
    transaction.
    """

    def __init__(self, database: str | Path | sqlite3.Connection):
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        if self._owns_connection:
            path = Path(database)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(path)
        else:
            self.conn = database
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    def __enter__(self) -> "AcquisitionLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_connection:
            self.conn.close()

    def _commit(self) -> None:
        if self._owns_connection:
            self.conn.commit()

    def migrate(self) -> None:
        """Apply the acquisition schema without changing ``DocumentEstate``."""

        self.conn.executescript(
            OUTBOX_DDL
            + """
            CREATE TABLE IF NOT EXISTS acquisition_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS acquisition_runs (
                run_id TEXT PRIMARY KEY,
                trigger_name TEXT NOT NULL,
                scope_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN
                    ('running','succeeded','partial','failed')),
                owner_id TEXT NOT NULL,
                lease_name TEXT,
                lease_ttl_seconds INTEGER NOT NULL DEFAULT 21600,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                stored_count INTEGER NOT NULL DEFAULT 0,
                unchanged_count INTEGER NOT NULL DEFAULT 0,
                retryable_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS acquisition_leases (
                lease_name TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                run_id TEXT NOT NULL REFERENCES acquisition_runs(run_id),
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS source_records (
                source_key TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                company TEXT NOT NULL,
                document_type TEXT NOT NULL,
                document_family_id TEXT NOT NULL,
                title TEXT,
                canonical_url TEXT,
                period TEXT,
                language TEXT,
                published_at TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                status TEXT NOT NULL CHECK(status IN
                    ('discovered','retryable','rejected','stored')),
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                first_seen_run_id TEXT REFERENCES acquisition_runs(run_id),
                last_seen_run_id TEXT REFERENCES acquisition_runs(run_id),
                current_document_id TEXT REFERENCES documents(document_id),
                current_content_sha256 TEXT,
                current_object_key TEXT,
                current_version INTEGER NOT NULL DEFAULT 0,
                rejection_reason TEXT,
                last_error TEXT,
                PRIMARY KEY(source_key, source_record_id)
            );
            CREATE TABLE IF NOT EXISTS source_record_versions (
                document_id TEXT PRIMARY KEY REFERENCES documents(document_id),
                document_family_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                source_key TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                object_key TEXT NOT NULL,
                supersedes_document_id TEXT REFERENCES documents(document_id),
                stored_at TEXT NOT NULL,
                UNIQUE(document_family_id, version),
                FOREIGN KEY(source_key, source_record_id)
                    REFERENCES source_records(source_key, source_record_id)
            );
            CREATE TABLE IF NOT EXISTS document_projects (
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                project TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY(document_id, project)
            );
            CREATE TABLE IF NOT EXISTS acquisition_attempts (
                attempt_id TEXT PRIMARY KEY,
                run_id TEXT REFERENCES acquisition_runs(run_id),
                source_key TEXT NOT NULL,
                source_record_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('discovered','retryable','rejected','stored')),
                outcome TEXT,
                content_sha256 TEXT,
                document_id TEXT REFERENCES documents(document_id),
                started_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                error TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                FOREIGN KEY(source_key, source_record_id)
                    REFERENCES source_records(source_key, source_record_id)
            );
            CREATE INDEX IF NOT EXISTS idx_source_records_company_period
                ON source_records(company, document_type, period);
            CREATE INDEX IF NOT EXISTS idx_source_records_status
                ON source_records(status);
            CREATE INDEX IF NOT EXISTS idx_source_versions_family
                ON source_record_versions(document_family_id, version);
            CREATE INDEX IF NOT EXISTS idx_document_projects_project
                ON document_projects(project, document_id);
            CREATE INDEX IF NOT EXISTS idx_attempts_run
                ON acquisition_attempts(run_id, status);
            CREATE TRIGGER IF NOT EXISTS source_record_versions_no_update
            BEFORE UPDATE ON source_record_versions
            BEGIN
                SELECT RAISE(ABORT, 'source_record_versions are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS source_record_versions_no_delete
            BEFORE DELETE ON source_record_versions
            BEGIN
                SELECT RAISE(ABORT, 'source_record_versions are immutable');
            END;
            """
        )
        # ``content_objects`` predates acquisition and keeps its absolute
        # compatibility path.  New rows additionally carry a portable key.
        tables = {
            row["name"]
            for row in self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "content_objects" in tables:
            columns = {
                row["name"]
                for row in self.conn.execute("PRAGMA table_info(content_objects)")
            }
            if "object_key" not in columns:
                self.conn.execute("ALTER TABLE content_objects ADD COLUMN object_key TEXT")
            self.conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS idx_content_objects_object_key
                   ON content_objects(object_key) WHERE object_key IS NOT NULL"""
            )
        run_columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(acquisition_runs)")
        }
        if "lease_ttl_seconds" not in run_columns:
            self.conn.execute(
                """ALTER TABLE acquisition_runs
                   ADD COLUMN lease_ttl_seconds INTEGER NOT NULL DEFAULT 21600"""
            )
        if "unchanged_count" not in run_columns:
            self.conn.execute(
                """ALTER TABLE acquisition_runs
                   ADD COLUMN unchanged_count INTEGER NOT NULL DEFAULT 0"""
            )
            self.conn.execute(
                """UPDATE acquisition_runs
                   SET stored_count=(
                           SELECT COUNT(*) FROM acquisition_attempts a
                           WHERE a.run_id=acquisition_runs.run_id
                             AND a.status='stored' AND a.outcome='new_version'
                       ),
                       unchanged_count=(
                           SELECT COUNT(*) FROM acquisition_attempts a
                           WHERE a.run_id=acquisition_runs.run_id
                             AND a.status='stored' AND a.outcome='same_hash'
                       )
                   WHERE status<>'running'"""
            )
        self.conn.execute(
            """INSERT OR IGNORE INTO acquisition_schema_migrations(version, applied_at)
               VALUES(1, ?)""",
            (utc_now(),),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO acquisition_schema_migrations(version, applied_at)
               VALUES(2, ?)""",
            (utc_now(),),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO acquisition_schema_migrations(version, applied_at)
               VALUES(3, ?)""",
            (utc_now(),),
        )
        self.conn.commit()

    @contextmanager
    def _immediate(self) -> Iterator[None]:
        if self.conn.in_transaction:
            yield
            return
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def start_run(
        self,
        *,
        trigger: str = "manual",
        scope: Mapping[str, Any] | None = None,
        run_id: str | None = None,
        lease_name: str = "quarterly-refresh",
        owner: str | None = None,
        lease_ttl_seconds: int = 21600,
    ) -> str:
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        run_id = run_id or uuid.uuid4().hex
        owner = owner or f"{socket.gethostname()}:{os.getpid()}"
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        expires = (now_dt + timedelta(seconds=lease_ttl_seconds)).isoformat(
            timespec="seconds"
        )
        with self._immediate():
            lease = self.conn.execute(
                "SELECT * FROM acquisition_leases WHERE lease_name=?",
                (lease_name,),
            ).fetchone()
            if lease and lease["expires_at"] > now:
                raise AcquisitionLeaseHeld(
                    f"acquisition lease {lease_name!r} is held by "
                    f"{lease['owner_id']} until {lease['expires_at']}"
                )
            if lease:
                self.conn.execute(
                    """UPDATE acquisition_runs
                       SET status='failed',completed_at=?,
                           error=COALESCE(error, ?)
                       WHERE run_id=? AND status='running'""",
                    (
                        now,
                        (
                            f"acquisition lease {lease_name!r} expired and was "
                            f"reclaimed by {owner}"
                        ),
                        lease["run_id"],
                    ),
                )
            self.conn.execute(
                """INSERT INTO acquisition_runs(
                       run_id,trigger_name,scope_json,status,owner_id,lease_name,
                       lease_ttl_seconds,started_at
                   ) VALUES(?,?,?,'running',?,?,?,?)""",
                (
                    run_id,
                    trigger,
                    _json(scope),
                    owner,
                    lease_name,
                    lease_ttl_seconds,
                    now,
                ),
            )
            self.conn.execute(
                """INSERT INTO acquisition_leases(
                       lease_name,owner_id,run_id,acquired_at,expires_at
                   ) VALUES(?,?,?,?,?)
                   ON CONFLICT(lease_name) DO UPDATE SET
                       owner_id=excluded.owner_id,run_id=excluded.run_id,
                       acquired_at=excluded.acquired_at,expires_at=excluded.expires_at""",
                (lease_name, owner, run_id, now, expires),
            )
        return run_id

    def renew_lease(
        self,
        run_id: str,
        *,
        owner: str | None = None,
        lease_ttl_seconds: int | None = None,
    ) -> str:
        """Extend a live run's lease using run and owner as fencing tokens."""

        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat(timespec="seconds")
        with self._immediate():
            run = self.conn.execute(
                """SELECT owner_id,lease_name,lease_ttl_seconds,status
                   FROM acquisition_runs WHERE run_id=?""",
                (run_id,),
            ).fetchone()
            if run is None:
                raise KeyError(f"unknown acquisition run: {run_id}")
            if run["status"] != "running" or not run["lease_name"]:
                raise AcquisitionLeaseLost(
                    f"acquisition run {run_id} no longer owns a live lease"
                )
            expected_owner = owner or run["owner_id"]
            if expected_owner != run["owner_id"]:
                raise AcquisitionLeaseLost(
                    f"acquisition run {run_id} is fenced to {run['owner_id']}"
                )
            ttl = (
                int(run["lease_ttl_seconds"])
                if lease_ttl_seconds is None
                else lease_ttl_seconds
            )
            if ttl <= 0:
                raise ValueError("lease_ttl_seconds must be positive")
            expires = (now_dt + timedelta(seconds=ttl)).isoformat(
                timespec="seconds"
            )
            cursor = self.conn.execute(
                """UPDATE acquisition_leases SET expires_at=?
                   WHERE lease_name=? AND run_id=? AND owner_id=?
                     AND expires_at>?""",
                (
                    expires,
                    run["lease_name"],
                    run_id,
                    expected_owner,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise AcquisitionLeaseLost(
                    f"acquisition run {run_id} lease expired or was reclaimed"
                )
        return expires

    heartbeat = renew_lease

    def assert_live_lease(self, run_id: str, *, owner: str | None = None) -> None:
        """Fence a mutation to the current, unexpired owner of ``run_id``."""

        now = utc_now()
        run = self.conn.execute(
            """SELECT owner_id,lease_name,status FROM acquisition_runs
               WHERE run_id=?""",
            (run_id,),
        ).fetchone()
        expected_owner = owner or (run["owner_id"] if run else None)
        lease = (
            self.conn.execute(
                """SELECT owner_id,run_id,expires_at FROM acquisition_leases
                   WHERE lease_name=?""",
                (run["lease_name"],),
            ).fetchone()
            if run and run["lease_name"]
            else None
        )
        if (
            run is None
            or run["status"] != "running"
            or lease is None
            or lease["run_id"] != run_id
            or lease["owner_id"] != expected_owner
            or lease["expires_at"] <= now
        ):
            raise AcquisitionLeaseLost(
                f"acquisition run {run_id} does not own a live fenced lease"
            )

    def finish_run(
        self,
        run_id: str,
        *,
        status: str = "succeeded",
        error: str | None = None,
    ) -> None:
        if status not in RUN_STATUSES - {"running"}:
            raise ValueError(f"invalid terminal run status: {status!r}")
        now = utc_now()
        with self._immediate():
            row = self.conn.execute(
                "SELECT status FROM acquisition_runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown acquisition run: {run_id}")
            if row["status"] != "running":
                if row["status"] == "failed" and status == "failed":
                    return
                raise ValueError(f"acquisition run {run_id} is already {row['status']}")
            # Terminal state and lease release are themselves fenced mutations.
            # A stale worker must not report success after another worker has
            # reclaimed the fleet lease.
            self.assert_live_lease(run_id)
            counts = {
                item["status"]: item["count"]
                for item in self.conn.execute(
                    """SELECT status,COUNT(*) AS count FROM acquisition_attempts
                       WHERE run_id=? GROUP BY status""",
                    (run_id,),
                )
            }
            stored_outcomes = {
                item["outcome"]: item["count"]
                for item in self.conn.execute(
                    """SELECT outcome,COUNT(*) AS count FROM acquisition_attempts
                       WHERE run_id=? AND status='stored' GROUP BY outcome""",
                    (run_id,),
                )
            }
            discovered = self.conn.execute(
                "SELECT COUNT(*) FROM source_records WHERE last_seen_run_id=?",
                (run_id,),
            ).fetchone()[0]
            self.conn.execute(
                """UPDATE acquisition_runs SET status=?,completed_at=?,
                       discovered_count=?,stored_count=?,unchanged_count=?,
                       retryable_count=?,
                       rejected_count=?,error=?
                   WHERE run_id=?""",
                (
                    status,
                    now,
                    discovered,
                    stored_outcomes.get("new_version", 0),
                    stored_outcomes.get("same_hash", 0),
                    counts.get("retryable", 0),
                    counts.get("rejected", 0),
                    error,
                    run_id,
                ),
            )
            self.conn.execute(
                "DELETE FROM acquisition_leases WHERE run_id=?", (run_id,)
            )

    def discover(self, source_record: object, *, run_id: str | None = None) -> sqlite3.Row:
        values = _record_values(source_record)
        now = utc_now()
        with self._immediate():
            if run_id is not None:
                self.assert_live_lease(run_id)
            self.conn.execute(
                """
                INSERT INTO source_records(
                    source_key,source_record_id,company,document_type,document_family_id,
                    title,canonical_url,period,language,published_at,metadata_json,status,
                    first_seen_at,last_seen_at,first_seen_run_id,last_seen_run_id
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'discovered',?,?,?,?)
                ON CONFLICT(source_key,source_record_id) DO UPDATE SET
                    company=excluded.company,document_type=excluded.document_type,
                    document_family_id=excluded.document_family_id,title=excluded.title,
                    canonical_url=excluded.canonical_url,period=excluded.period,
                    language=excluded.language,published_at=excluded.published_at,
                    metadata_json=excluded.metadata_json,last_seen_at=excluded.last_seen_at,
                    last_seen_run_id=excluded.last_seen_run_id,
                    status=CASE WHEN source_records.status='stored'
                                THEN 'stored' ELSE 'discovered' END,
                    rejection_reason=NULL,last_error=NULL
                """,
                (
                    values["source_key"],
                    values["source_record_id"],
                    values["company"],
                    values["document_type"],
                    values["document_family_id"],
                    values["title"],
                    values["canonical_url"],
                    values["period"],
                    values["language"],
                    values["published_at"],
                    values["metadata_json"],
                    now,
                    now,
                    run_id,
                    run_id,
                ),
            )
        return self.get(values["source_key"], values["source_record_id"])

    def get(self, source_key: str, source_record_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            """SELECT * FROM source_records
               WHERE source_key=? AND source_record_id=?""",
            (source_key, source_record_id),
        ).fetchone()
        if row is None:
            raise KeyError(f"unknown source record: {source_key}/{source_record_id}")
        return row

    def _mark(
        self,
        source_record: object,
        *,
        status: str,
        run_id: str | None,
        error: str,
        attempt_id: str | None,
    ) -> str:
        if status not in {"retryable", "rejected"}:
            raise ValueError(f"unsupported acquisition outcome: {status}")
        values = _record_values(source_record)
        attempt_id = attempt_id or uuid.uuid4().hex
        now = utc_now()
        with self._immediate():
            if run_id is not None:
                self.assert_live_lease(run_id)
            # ``discover`` participates in this transaction, so a reclaim
            # cannot land between observation and outcome publication.
            self.discover(source_record, run_id=run_id)
            self.conn.execute(
                """UPDATE source_records SET status=?,rejection_reason=?,last_error=?,
                       last_seen_at=?,last_seen_run_id=?
                   WHERE source_key=? AND source_record_id=?""",
                (
                    status,
                    error if status == "rejected" else None,
                    error,
                    now,
                    run_id,
                    values["source_key"],
                    values["source_record_id"],
                ),
            )
            self.conn.execute(
                """INSERT INTO acquisition_attempts(
                       attempt_id,run_id,source_key,source_record_id,status,outcome,
                       started_at,completed_at,error
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    attempt_id,
                    run_id,
                    values["source_key"],
                    values["source_record_id"],
                    status,
                    status,
                    now,
                    now,
                    error,
                ),
            )
        return attempt_id

    def mark_retryable(
        self,
        source_record: object,
        *,
        run_id: str | None,
        error: str,
        attempt_id: str | None = None,
    ) -> str:
        return self._mark(
            source_record,
            status="retryable",
            run_id=run_id,
            error=error,
            attempt_id=attempt_id,
        )

    def mark_rejected(
        self,
        source_record: object,
        *,
        run_id: str | None,
        reason: str,
        attempt_id: str | None = None,
    ) -> str:
        return self._mark(
            source_record,
            status="rejected",
            run_id=run_id,
            error=reason,
            attempt_id=attempt_id,
        )

    def known_periods(
        self,
        issuer_slug: str,
        document_type: str = "quarterly_release",
    ) -> set[str]:
        rows = self.conn.execute(
            """SELECT DISTINCT period FROM source_records
               WHERE company=? AND document_type=? AND status='stored'
                 AND period IS NOT NULL""",
            (issuer_slug, document_type),
        )
        return {row["period"] for row in rows}
