"""Atomic, content-addressed writer for newly acquired estate documents."""

from __future__ import annotations

import errno
import gzip
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from src.acquisition.ledger import AcquisitionLedger, _record_values, utc_now
from src.shared.document_estate import (
    DocumentEstate,
    EstateDocument,
    file_sha256,
)


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_ROUTING_CHANGED_EVENT = "estate.document.routing_changed"
_MIME_SUFFIXES = {
    "application/pdf": ".pdf",
    "application/json": ".json",
    "application/zip": ".zip",
    "application/xml": ".xml",
    "text/html": ".html",
    "text/markdown": ".md",
    "text/plain": ".txt",
}


class ArtifactValidationError(ValueError):
    """Fetched bytes do not match the declared durable document format."""


# Compatibility for the brief development name; callers should use
# ``ArtifactValidationError`` as the stable public contract.
PayloadValidationError = ArtifactValidationError


@dataclass(frozen=True, slots=True)
class StoreResult:
    created: bool
    document_id: str
    version: int
    sha256: str
    object_key: str
    artifact_path: Path
    supersedes_document_id: str | None


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value.strip()).strip("._-")
    return cleaned[:120] or fallback


def _suffix(filename: str | None, media_type: str) -> str:
    candidate = Path(filename).suffix.lower() if filename else ""
    if candidate and len(candidate) <= 10 and candidate[1:].isalnum():
        return candidate
    return _MIME_SUFFIXES.get(media_type.lower(), ".bin")


def _validate_payload(content: bytes, filename: str | None, media_type: str) -> None:
    suffix = Path(filename).suffix.lower() if filename else ""
    normalized_type = media_type.lower().split(";", 1)[0].strip()
    if normalized_type == "application/pdf" or suffix == ".pdf":
        if not content.startswith(b"%PDF-"):
            raise ArtifactValidationError(
                "declared PDF does not start with the PDF magic signature"
            )
    if normalized_type in {"application/zip", "application/x-zip-compressed"}:
        if not content.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
            raise ArtifactValidationError(
                "declared ZIP does not start with a ZIP magic signature"
            )
    if normalized_type in {"application/gzip", "application/x-gzip"} or suffix == ".gz":
        if not content.startswith(b"\x1f\x8b"):
            raise ArtifactValidationError(
                "declared gzip does not start with the gzip magic signature"
            )
        try:
            decompressed = gzip.decompress(content)
        except (EOFError, OSError) as exc:
            raise ArtifactValidationError("declared gzip is not readable") from exc
        if filename and filename.lower().endswith(".json.gz"):
            try:
                json.loads(decompressed.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactValidationError(
                    "gzipped JSON is not valid UTF-8 JSON"
                ) from exc
    if normalized_type == "application/json" or suffix == ".json":
        try:
            json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactValidationError("declared JSON is not valid UTF-8 JSON") from exc


def _exclusive_bytes(target: Path, content: bytes) -> None:
    """Publish bytes atomically without ever replacing an existing object."""

    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".incoming-", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)


def _link_or_copy(source: Path, target: Path) -> None:
    """Create one immutable compatibility path, preferring a hard link."""

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
        return
    except FileExistsError:
        return
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
            raise
    _exclusive_bytes(target, source.read_bytes())


def _project_memberships(
    memberships: Iterable[Mapping[str, object]],
) -> tuple[str, ...]:
    projects: set[str] = set()
    aliases = {"alpha_go": "alpha-go", "soft": "soft", "earnings": "earnings"}
    for membership in memberships:
        for key, project in aliases.items():
            if membership.get(key) is True:
                projects.add(project)
        explicit = membership.get("project")
        if isinstance(explicit, str) and explicit.strip():
            projects.add(explicit.strip())
        listed = membership.get("projects")
        if isinstance(listed, (list, tuple, set, frozenset)):
            projects.update(
                str(project).strip() for project in listed if str(project).strip()
            )
    return tuple(sorted(projects))


class EstateWriter:
    """The sole write boundary between acquisition adapters and the estate."""

    def __init__(self, database: str | Path, estate_root: str | Path):
        self.estate = DocumentEstate(database)
        self.estate_root = Path(estate_root).resolve()
        self.estate_root.mkdir(parents=True, exist_ok=True)
        self.ledger = AcquisitionLedger(self.estate.conn)

    def __enter__(self) -> "EstateWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.estate.close()

    def store_fetched(
        self,
        fetched_artifact: object,
        *,
        run_id: str | None = None,
        memberships: Iterable[Mapping[str, object]] = (),
    ) -> StoreResult:
        source = getattr(fetched_artifact, "source")
        content = getattr(fetched_artifact, "content")
        if not isinstance(content, bytes) or not content:
            raise ValueError("fetched artifact content must be non-empty bytes")
        return self.store(
            source,
            content,
            filename=getattr(fetched_artifact, "filename", None),
            media_type=str(
                getattr(fetched_artifact, "media_type", "application/octet-stream")
            ),
            role=str(getattr(fetched_artifact, "role", "original")),
            run_id=run_id,
            memberships=memberships,
            attempt_metadata={
                "fetched_at": str(getattr(fetched_artifact, "fetched_at", "")),
                "response_status": getattr(fetched_artifact, "response_status", None),
                "response_headers": dict(
                    getattr(fetched_artifact, "response_headers", {}) or {}
                ),
            },
        )

    def store(
        self,
        source_record: object,
        content: bytes,
        *,
        filename: str | None = None,
        media_type: str = "application/octet-stream",
        role: str = "original",
        run_id: str | None = None,
        memberships: Iterable[Mapping[str, object]] = (),
        attempt_metadata: Mapping[str, object] | None = None,
    ) -> StoreResult:
        """Store one fetched artifact, idempotently and without replacement."""

        values = _record_values(source_record)
        membership_rows = tuple(dict(row) for row in memberships)
        projects = _project_memberships(membership_rows)
        _validate_payload(content, filename, media_type)
        digest = hashlib.sha256(content).hexdigest()
        object_key = f"blobs/{digest[:2]}/{digest}"
        blob = self.estate_root / object_key
        _exclusive_bytes(blob, content)
        if file_sha256(blob) != digest:
            raise RuntimeError(f"content object failed hash verification: {object_key}")

        conn = self.estate.conn
        if conn.in_transaction:
            raise RuntimeError(
                "EstateWriter.store cannot run inside an existing caller transaction"
            )
        conn.execute("BEGIN IMMEDIATE")
        try:
            self.ledger.discover(source_record, run_id=run_id)
            current = self.ledger.get(
                values["source_key"], values["source_record_id"]
            )
            family_latest = conn.execute(
                """SELECT * FROM source_record_versions
                   WHERE document_family_id=? ORDER BY version DESC LIMIT 1""",
                (values["document_family_id"],),
            ).fetchone()

            # The same bytes at either the same source identity or a new URL
            # for the same filing family are an idempotent observation.
            unchanged = None
            if current["current_content_sha256"] == digest:
                unchanged = current
            elif family_latest and family_latest["content_sha256"] == digest:
                unchanged = family_latest
            if unchanged is not None:
                document_id = (
                    unchanged["current_document_id"]
                    if "current_document_id" in unchanged.keys()
                    else unchanged["document_id"]
                )
                version = (
                    unchanged["current_version"]
                    if "current_version" in unchanged.keys()
                    else unchanged["version"]
                )
                artifact = conn.execute(
                    """SELECT path FROM artifacts
                       WHERE document_id=? AND role=? ORDER BY created_at LIMIT 1""",
                    (document_id, role),
                ).fetchone()
                artifact_path = (
                    self._catalog_artifact_path(artifact["path"])
                    if artifact
                    else None
                )
                if artifact_path is None or not artifact_path.is_file():
                    artifact_path = self._compatibility_path(
                        values,
                        blob,
                        document_id=document_id,
                        digest=digest,
                        version=version,
                        extension=_suffix(filename, media_type),
                        media_type=media_type,
                    )
                    repaired_digest = self.estate.add_artifact(
                        document_id,
                        artifact_path,
                        project="acquisition",
                        role=role,
                        portable_root=self.estate_root,
                    )
                    if (
                        repaired_digest != digest
                        or self.estate.artifact_document(
                            artifact_path,
                            portable_root=self.estate_root,
                        )
                        != document_id
                    ):
                        raise RuntimeError(
                            f"could not repair {role!r} artifact for {document_id}"
                        )
                now = utc_now()
                conn.execute(
                    """UPDATE source_records SET status='stored',
                           current_document_id=?,current_content_sha256=?,
                           current_object_key=?,current_version=?,
                           rejection_reason=NULL,last_error=NULL,last_seen_at=?,
                           last_seen_run_id=?
                       WHERE source_key=? AND source_record_id=?""",
                    (
                        document_id,
                        digest,
                        object_key,
                        version,
                        now,
                        run_id,
                        values["source_key"],
                        values["source_record_id"],
                    ),
                )
                self._record_attempt(
                    source_record,
                    run_id=run_id,
                    outcome="same_hash",
                    digest=digest,
                    document_id=document_id,
                    metadata=attempt_metadata,
                )
                added_projects = self._pin_projects(
                    document_id, projects, now=now
                )
                memberships_changed = self._upsert_existing_memberships(
                    document_id,
                    membership_rows,
                    fallback_company=values["company"],
                )
                if added_projects or memberships_changed:
                    current_projects = [
                        row["project"]
                        for row in conn.execute(
                            """SELECT project FROM document_projects
                               WHERE document_id=? ORDER BY project""",
                            (document_id,),
                        )
                    ]
                    current_memberships = [
                        {
                            "company": row["company"],
                            "industry": row["industry"],
                        }
                        for row in conn.execute(
                            """SELECT company,industry FROM memberships
                               WHERE document_id=? ORDER BY company""",
                            (document_id,),
                        )
                    ]
                    event_id = uuid.uuid4().hex
                    conn.execute(
                        """INSERT INTO outbox(
                               event_id,event_type,aggregate_id,dedupe_key,
                               payload_json,created_at,available_at
                           ) VALUES(?,?,?,?,?,?,?)""",
                        (
                            event_id,
                            _ROUTING_CHANGED_EVENT,
                            document_id,
                            f"{_ROUTING_CHANGED_EVENT}:{document_id}:{event_id}",
                            json.dumps(
                                {
                                    "schema_version": 1,
                                    "document_id": document_id,
                                    "added_projects": list(added_projects),
                                    "memberships_changed": memberships_changed,
                                    "projects": current_projects,
                                    "memberships": current_memberships,
                                },
                                sort_keys=True,
                            ),
                            now,
                            now,
                        ),
                    )
                if run_id is not None:
                    self.ledger.assert_live_lease(run_id)
                conn.commit()
                return StoreResult(
                    created=False,
                    document_id=document_id,
                    version=version,
                    sha256=digest,
                    object_key=object_key,
                    artifact_path=artifact_path,
                    supersedes_document_id=(
                        family_latest["supersedes_document_id"]
                        if family_latest else None
                    ),
                )

            previous_document_id = (
                family_latest["document_id"] if family_latest else None
            )
            version = int(family_latest["version"]) + 1 if family_latest else 1
            document_id = "acq:" + hashlib.sha256(
                (
                    f"{values['document_family_id']}\0{version}\0{digest}"
                ).encode("utf-8")
            ).hexdigest()
            extension = _suffix(filename, media_type)
            artifact_path = self._compatibility_path(
                values,
                blob,
                document_id=document_id,
                digest=digest,
                version=version,
                extension=extension,
                media_type=media_type,
            )
            now = utc_now()
            stat = blob.stat()
            conn.execute(
                """INSERT INTO content_objects(
                       sha256,blob_path,size_bytes,st_dev,st_ino,verified_at,object_key
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (
                    digest,
                    str(blob),
                    len(content),
                    stat.st_dev,
                    stat.st_ino,
                    now,
                    object_key,
                ),
            )
            existing_object = conn.execute(
                "SELECT * FROM content_objects WHERE sha256=?", (digest,)
            ).fetchone()
            if existing_object["object_key"] is None:
                conn.execute(
                    "UPDATE content_objects SET object_key=? WHERE sha256=?",
                    (object_key, digest),
                )
            elif existing_object["object_key"] != object_key:
                raise RuntimeError(
                    f"content object {digest} has conflicting portable key"
                )

            metadata = dict(getattr(source_record, "metadata", {}) or {})
            metadata["acquisition"] = {
                "source_key": values["source_key"],
                "source_record_id": values["source_record_id"],
                "document_family_id": values["document_family_id"],
                "version": version,
                "supersedes_document_id": previous_document_id,
                "content_sha256": digest,
                "object_key": object_key,
                "media_type": media_type,
            }
            self.estate.upsert_document(
                EstateDocument(
                    document_id=document_id,
                    company=values["company"],
                    period=values["period"],
                    doc_type=values["document_type"],
                    title=values["title"]
                    or filename
                    or f"{values['company']} {values['document_type']}",
                    language=values["language"],
                    source_url=values["canonical_url"],
                    published_at=values["published_at"],
                    metadata=metadata,
                ),
                memberships=list(membership_rows)
                or [{"company": values["company"], "industry": None}],
            )
            self.estate.add_project_record(
                "acquisition",
                (
                    f"{values['source_key']}/{values['source_record_id']}"
                    f"/v{version}"
                ),
                document_id,
            )
            artifact_digest = self.estate.add_artifact(
                document_id,
                artifact_path,
                project="acquisition",
                role=role,
                portable_root=self.estate_root,
            )
            if artifact_digest != digest:
                raise RuntimeError("compatibility artifact differs from content object")
            artifact_owner = self.estate.artifact_document(
                artifact_path,
                portable_root=self.estate_root,
            )
            if artifact_owner != document_id:
                raise RuntimeError(
                    "compatibility artifact is catalog-owned by "
                    f"{artifact_owner}, expected {document_id}"
                )
            conn.execute(
                """INSERT INTO source_record_versions(
                       document_id,document_family_id,version,source_key,
                       source_record_id,content_sha256,object_key,
                       supersedes_document_id,stored_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    document_id,
                    values["document_family_id"],
                    version,
                    values["source_key"],
                    values["source_record_id"],
                    digest,
                    object_key,
                    previous_document_id,
                    now,
                ),
            )
            conn.execute(
                """UPDATE source_records SET status='stored',
                       current_document_id=?,current_content_sha256=?,
                       current_object_key=?,current_version=?,
                       rejection_reason=NULL,last_error=NULL,last_seen_at=?,
                       last_seen_run_id=?
                   WHERE source_key=? AND source_record_id=?""",
                (
                    document_id,
                    digest,
                    object_key,
                    version,
                    now,
                    run_id,
                    values["source_key"],
                    values["source_record_id"],
                ),
            )
            self._record_attempt(
                source_record,
                run_id=run_id,
                outcome="new_version",
                digest=digest,
                document_id=document_id,
                metadata=attempt_metadata,
            )
            self._pin_projects(document_id, projects, now=now)
            payload = {
                "schema_version": 1,
                "document_id": document_id,
                "document_family_id": values["document_family_id"],
                "version": version,
                "supersedes_document_id": previous_document_id,
                "company": values["company"],
                "document_type": values["document_type"],
                "period": values["period"],
                "language": values["language"],
                "content_sha256": digest,
                "object_key": object_key,
                "media_type": media_type,
                "artifact_role": role,
                "source_key": values["source_key"],
                "source_record_id": values["source_record_id"],
                "projects": list(projects),
            }
            conn.execute(
                """INSERT INTO outbox(
                       event_id,event_type,aggregate_id,dedupe_key,payload_json,
                       created_at,available_at
                   ) VALUES(?,?,?,?,?,?,?)""",
                (
                    uuid.uuid4().hex,
                    "estate.document.stored",
                    document_id,
                    f"estate.document.stored:{document_id}",
                    json.dumps(payload, sort_keys=True),
                    now,
                    now,
                ),
            )
            if run_id is not None:
                self.ledger.assert_live_lease(run_id)
            conn.commit()
            return StoreResult(
                created=True,
                document_id=document_id,
                version=version,
                sha256=digest,
                object_key=object_key,
                artifact_path=artifact_path,
                supersedes_document_id=previous_document_id,
            )
        except BaseException:
            conn.rollback()
            raise

    def _pin_projects(
        self,
        document_id: str,
        projects: Iterable[str],
        *,
        now: str,
    ) -> tuple[str, ...]:
        added: list[str] = []
        for project in projects:
            cursor = self.estate.conn.execute(
                """INSERT OR IGNORE INTO document_projects(
                       document_id,project,added_at
                   ) VALUES(?,?,?)""",
                (document_id, project, now),
            )
            if cursor.rowcount == 1:
                added.append(project)
        return tuple(added)

    def _upsert_existing_memberships(
        self,
        document_id: str,
        memberships: Iterable[Mapping[str, object]],
        *,
        fallback_company: str,
    ) -> bool:
        rows = tuple(dict(row) for row in memberships)
        if not rows:
            return False
        before = {
            (row["company"], row["industry"])
            for row in self.estate.conn.execute(
                """SELECT company,industry FROM memberships
                   WHERE document_id=?""",
                (document_id,),
            )
        }
        self.estate.add_memberships(
            document_id,
            rows,
            fallback_company=fallback_company,
        )
        after = {
            (row["company"], row["industry"])
            for row in self.estate.conn.execute(
                """SELECT company,industry FROM memberships
                   WHERE document_id=?""",
                (document_id,),
            )
        }
        return before != after

    def _compatibility_path(
        self,
        values: Mapping[str, object],
        blob: Path,
        *,
        document_id: str,
        digest: str,
        version: int,
        extension: str,
        media_type: str,
    ) -> Path:
        company = _safe_component(str(values["company"]), "unknown")
        base = _safe_component(
            str(values.get("period") or values["source_record_id"]), digest[:12]
        )
        normalized_type = media_type.lower().split(";", 1)[0].strip()
        if (
            values["document_type"] == "quarterly_release"
            and normalized_type == "application/pdf"
        ):
            namespace = Path("reports")
        elif values["document_type"] == "regulatory_filing":
            namespace = Path("regulatory")
        else:
            namespace = Path("artifacts") / _safe_component(
                str(values["document_type"]), "document"
            )
        directory = self.estate_root / "views" / namespace / company
        candidates = [
            directory / f"{base}{extension}",
            directory
            / f"{base}__{_safe_component(str(values['document_type']), 'document')}"
            f"__v{version}{extension}",
            directory / f"{base}__acq_{document_id[-12:]}{extension}",
        ]
        for candidate in candidates:
            if candidate.exists():
                owner = self.estate.artifact_document(
                    candidate,
                    portable_root=self.estate_root,
                )
                if file_sha256(candidate) == digest and owner in {None, document_id}:
                    return candidate
                continue
            _link_or_copy(blob, candidate)
            owner = self.estate.artifact_document(
                candidate,
                portable_root=self.estate_root,
            )
            if file_sha256(candidate) == digest and owner in {None, document_id}:
                return candidate
        raise RuntimeError(f"could not allocate immutable compatibility path for {base}")

    def _catalog_artifact_path(self, raw: object) -> Path:
        path = Path(str(raw))
        return path if path.is_absolute() else self.estate_root / path

    def _record_attempt(
        self,
        source_record: object,
        *,
        run_id: str | None,
        outcome: str,
        digest: str,
        document_id: str,
        metadata: Mapping[str, object] | None,
    ) -> None:
        values = _record_values(source_record)
        now = utc_now()
        self.estate.conn.execute(
            """INSERT INTO acquisition_attempts(
                   attempt_id,run_id,source_key,source_record_id,status,outcome,
                   content_sha256,document_id,started_at,completed_at,metadata_json
               ) VALUES(?,?,?,?,'stored',?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                run_id,
                values["source_key"],
                values["source_record_id"],
                outcome,
                digest,
                document_id,
                now,
                now,
                json.dumps(dict(metadata or {}), sort_keys=True, default=str),
            ),
        )
