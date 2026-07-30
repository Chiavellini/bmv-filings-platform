"""Idempotent document derivatives for the shared estate.

The first supported derivative is an original PDF parsed to UTF-8 Markdown.
XBRL derivation is intentionally not implemented here: regulatory artifacts
are skipped with an explicit result so they can be routed to a purpose-built
facts consumer later.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from src.acquisition.ledger import OUTBOX_DDL
from src.shared.document_estate import (
    ArtifactRef,
    DocumentEstate,
    EstateReader,
    file_sha256,
)


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_STORED_EVENT = "estate.document.stored"
_ROUTING_CHANGED_EVENT = "estate.document.routing_changed"
_PARSED_EVENT = "estate.document.parsed"
_SUPPORTED_ORIGINAL_FORMATS = frozenset(
    {"pdf", "html", "htm", "md", "markdown", "txt", "text"}
)


class DerivativeInputError(RuntimeError):
    """The catalogued source or an existing derivative failed verification."""


@dataclass(frozen=True, slots=True)
class DerivativeResult:
    """Small handler result compatible with succeeded/skipped worker outcomes."""

    status: Literal["succeeded", "skipped"]
    document_id: str | None
    reason: str | None = None
    derivation_id: str | None = None
    output_artifact_id: str | None = None
    output_sha256: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def error(self) -> str | None:
        """Outbox-dispatcher compatibility; skipped reasons are non-fatal."""
        return self.reason

    @property
    def retry_after_seconds(self) -> None:
        return None


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_component(value: str, fallback: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value.strip()).strip("._-")
    return cleaned[:120] or fallback


def _event_value(event: object, name: str, default: object = None) -> object:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _event_payload(event: object) -> dict[str, Any]:
    payload = _event_value(event, "payload")
    if payload is None:
        payload = _event_value(event, "payload_json")
    if payload is None:
        return {}
    if isinstance(payload, str):
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("outbox payload must decode to a JSON object")
        return decoded
    if isinstance(payload, Mapping):
        return dict(payload)
    raise TypeError("outbox payload must be a mapping or JSON object string")


def _default_pdf_parser(path: Path) -> object:
    from src.parse.parse_pdf import parse_pdf

    return parse_pdf(path)


def _markdown_from_result(result: object) -> str:
    if isinstance(result, str):
        markdown = result
    elif isinstance(result, bytes):
        markdown = result.decode("utf-8")
    elif isinstance(result, Sequence) and not isinstance(result, (str, bytes)):
        if not result:
            raise ValueError("PDF parser returned an empty sequence")
        first = result[0]
        markdown = first.decode("utf-8") if isinstance(first, bytes) else first
    else:
        raise TypeError("PDF parser must return Markdown or a tuple beginning with Markdown")
    if not isinstance(markdown, str):
        raise TypeError("PDF parser result does not begin with Markdown text")
    if not markdown.strip():
        raise ValueError("PDF parser returned empty Markdown")
    return markdown


def _decode_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _html_to_markdown(path: Path) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(_decode_text(path), "html.parser")
    for element in soup(["script", "style", "noscript", "template"]):
        element.decompose()
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    lines = [
        line.strip()
        for line in soup.get_text("\n").splitlines()
        if line.strip()
    ]
    body = "\n\n".join(lines)
    if title and (not lines or lines[0] != title):
        body = f"# {title}\n\n{body}"
    if not body.strip():
        raise ValueError("HTML original contains no searchable text")
    return body


def _publish_bytes(target: Path, content: bytes) -> None:
    """Publish immutable bytes without replacing an existing file."""
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
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
        return
    except FileExistsError:
        return
    except OSError as exc:
        if exc.errno not in {errno.EXDEV, errno.EPERM, errno.EACCES}:
            raise
    _publish_bytes(target, source.read_bytes())


class PdfMarkdownDerivativeConsumer:
    """Create one verified Markdown derivative for an original PDF.

    ``parser`` is injected to make the expensive/external parsing boundary
    explicit. Its version is part of the derivation identity; callers must bump
    ``parser_version`` whenever parser behavior changes materially.
    """

    event_types = (_STORED_EVENT, _ROUTING_CHANGED_EVENT)

    def __init__(
        self,
        database: str | Path,
        estate_root: str | Path,
        *,
        parser: Callable[[Path], object] | None = None,
        parser_name: str = "root.parse_pdf",
        parser_version: str = "1",
    ):
        self.database = Path(database)
        self.estate_root = Path(estate_root).resolve()
        self.estate_root.mkdir(parents=True, exist_ok=True)
        self.parser = parser or _default_pdf_parser
        self.parser_name = parser_name.strip()
        self.parser_version = parser_version.strip()
        if not self.parser_name or not self.parser_version:
            raise ValueError("parser_name and parser_version are required")
        contract_digest = hashlib.sha256(
            f"{self.parser_name}\0{self.parser_version}".encode("utf-8")
        ).hexdigest()[:16]
        self.consumer_id = f"root.pdf-markdown.v1:{contract_digest}"
        self.estate = DocumentEstate(self.database)
        self.estate.conn.execute("PRAGMA busy_timeout=5000")
        self._migrate()

    def __enter__(self) -> "PdfMarkdownDerivativeConsumer":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.estate.close()

    def _migrate(self) -> None:
        conn = self.estate.conn
        content_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(content_objects)")
        }
        if "object_key" not in content_columns:
            conn.execute("ALTER TABLE content_objects ADD COLUMN object_key TEXT")
        conn.executescript(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_content_objects_object_key
                ON content_objects(object_key) WHERE object_key IS NOT NULL;
            CREATE TABLE IF NOT EXISTS document_derivations (
                derivation_id TEXT PRIMARY KEY,
                document_id TEXT NOT NULL REFERENCES documents(document_id),
                derivative_kind TEXT NOT NULL,
                input_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                input_sha256 TEXT NOT NULL,
                processor_name TEXT NOT NULL,
                processor_version TEXT NOT NULL,
                output_artifact_id TEXT NOT NULL REFERENCES artifacts(artifact_id),
                output_sha256 TEXT NOT NULL,
                output_object_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(
                    input_artifact_id,input_sha256,derivative_kind,
                    processor_name,processor_version
                )
            );
            CREATE INDEX IF NOT EXISTS idx_document_derivations_document
                ON document_derivations(document_id, derivative_kind);
            CREATE TRIGGER IF NOT EXISTS document_derivations_no_update
            BEFORE UPDATE ON document_derivations
            BEGIN
                SELECT RAISE(ABORT, 'document_derivations are immutable');
            END;
            CREATE TRIGGER IF NOT EXISTS document_derivations_no_delete
            BEFORE DELETE ON document_derivations
            BEGIN
                SELECT RAISE(ABORT, 'document_derivations are immutable');
            END;
            """
            # ``outbox`` is owned by the acquisition ledger; execute its canonical
            # definition so this consumer can run standalone without forking the schema.
            + OUTBOX_DDL
        )
        conn.commit()

    def handle(
        self, event: object, context: object | None = None
    ) -> DerivativeResult:
        """Handle a duck-typed outbox event."""
        del context  # accepted for the shared outbox dispatcher contract
        event_type = str(_event_value(event, "event_type", ""))
        payload = _event_payload(event)
        document_id = payload.get("document_id") or _event_value(
            event, "aggregate_id"
        )
        if event_type not in self.event_types:
            return DerivativeResult(
                status="skipped",
                document_id=str(document_id) if document_id else None,
                reason="unsupported_event",
            )
        if not document_id:
            raise ValueError("document event has no document_id")
        return self.process(
            str(document_id),
            emit_existing_route=event_type == _ROUTING_CHANGED_EVENT,
        )

    def process(
        self,
        document_id: str,
        *,
        emit_existing_route: bool = False,
    ) -> DerivativeResult:
        """Parse one original PDF and durably register its Markdown derivative."""
        with EstateReader(self.database) as reader:
            document = reader.document(document_id)
            if document is None:
                raise KeyError(f"estate document not found: {document_id}")
            if document.doc_type == "regulatory_filing":
                return DerivativeResult(
                    "skipped", document_id, reason="xbrl_not_supported"
                )
            originals = [
                ref
                for ref in reader.artifacts_for_document(
                    document_id, role="original"
                )
                if ref.format.lower() in _SUPPORTED_ORIGINAL_FORMATS
            ]
            originals.sort(
                key=lambda ref: (
                    {
                        "pdf": 0,
                        "html": 1,
                        "htm": 1,
                        "md": 2,
                        "markdown": 2,
                        "txt": 3,
                        "text": 3,
                    }.get(ref.format.lower(), 9),
                    str(ref.path),
                )
            )
            if not originals:
                xbrl_artifacts = reader.artifacts_for_document(document_id)
                if any(
                    ref.role == "raw_xbrl"
                    or ref.format in {"xbrl", "xml", "zip", "gz", "json"}
                    for ref in xbrl_artifacts
                ):
                    reason = "xbrl_not_supported"
                else:
                    reason = "no_supported_original"
                return DerivativeResult("skipped", document_id, reason=reason)
            source = originals[0]
            selected_projects = sorted(
                {
                    str(project).strip()
                    for project in reader.projects_for_document(document_id)
                    if str(project).strip()
                }
            )

        input_artifact_id = self._artifact_id(source)
        self._verify_source(source)
        source_format = source.format.lower()
        processor_name = (
            self.parser_name
            if source_format == "pdf"
            else f"root.{source_format}-to-markdown"
        )
        derivation_id = hashlib.sha256(
            (
                f"parsed_text\0{input_artifact_id}\0{source.sha256}\0"
                f"{processor_name}\0{self.parser_version}"
            ).encode("utf-8")
        ).hexdigest()
        existing = self._existing_result(derivation_id)
        if existing is not None:
            if emit_existing_route:
                return self._route_existing(
                    document_id,
                    derivation_id,
                    selected_projects,
                    existing,
                )
            return existing

        if source_format == "pdf":
            markdown = _markdown_from_result(self.parser(source.path))
        elif source_format in {"html", "htm"}:
            markdown = _html_to_markdown(source.path)
        else:
            markdown = _markdown_from_result(_decode_text(source.path))
        content = markdown.encode("utf-8")
        output_sha256 = hashlib.sha256(content).hexdigest()
        object_key = f"blobs/{output_sha256[:2]}/{output_sha256}"
        blob = self.estate_root / object_key
        _publish_bytes(blob, content)
        if file_sha256(blob) != output_sha256:
            raise DerivativeInputError(
                f"Markdown content object failed verification: {object_key}"
            )
        output_path = self._output_path(
            document.company, document.period, document_id, output_sha256
        )
        _link_or_copy(blob, output_path)
        if file_sha256(output_path) != output_sha256:
            raise DerivativeInputError(
                f"Markdown compatibility path failed verification: {output_path}"
            )

        conn = self.estate.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            concurrent = self._existing_result(derivation_id)
            if concurrent is not None:
                conn.rollback()
                if emit_existing_route:
                    return self._route_existing(
                        document_id,
                        derivation_id,
                        selected_projects,
                        concurrent,
                    )
                return concurrent

            now = _now()
            stat = blob.stat()
            conn.execute(
                """INSERT INTO content_objects(
                       sha256,blob_path,size_bytes,st_dev,st_ino,verified_at,object_key
                   ) VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(sha256) DO NOTHING""",
                (
                    output_sha256,
                    str(blob),
                    len(content),
                    stat.st_dev,
                    stat.st_ino,
                    now,
                    object_key,
                ),
            )
            stored_object = conn.execute(
                """SELECT blob_path,object_key FROM content_objects
                   WHERE sha256=?""",
                (output_sha256,),
            ).fetchone()
            if stored_object["object_key"] is None:
                conn.execute(
                    "UPDATE content_objects SET object_key=? WHERE sha256=?",
                    (object_key, output_sha256),
                )
            elif stored_object["object_key"] != object_key:
                raise RuntimeError(
                    f"content object {output_sha256} has conflicting object key"
                )

            artifact_sha = self.estate.add_artifact(
                document_id,
                output_path,
                project="root",
                role="parsed_text",
                portable_root=self.estate_root,
            )
            if artifact_sha != output_sha256:
                raise RuntimeError("parsed-text artifact differs from content object")
            output_artifact_id = self._artifact_id_for_path(output_path)
            if (
                self.estate.artifact_document(
                    output_path,
                    portable_root=self.estate_root,
                )
                != document_id
            ):
                raise RuntimeError(
                    f"parsed-text path is owned by another estate document: {output_path}"
                )

            conn.execute(
                """INSERT INTO document_derivations(
                       derivation_id,document_id,derivative_kind,input_artifact_id,
                       input_sha256,processor_name,processor_version,
                       output_artifact_id,output_sha256,output_object_key,created_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    derivation_id,
                    document_id,
                    "parsed_text",
                    input_artifact_id,
                    source.sha256,
                    processor_name,
                    self.parser_version,
                    output_artifact_id,
                    output_sha256,
                    object_key,
                    now,
                ),
            )
            self._insert_parsed_event_locked(
                document_id,
                derivation_id,
                selected_projects,
                now=now,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

        return DerivativeResult(
            status="succeeded",
            document_id=document_id,
            derivation_id=derivation_id,
            output_artifact_id=output_artifact_id,
            output_sha256=output_sha256,
        )

    def _route_existing(
        self,
        document_id: str,
        derivation_id: str,
        projects: Sequence[str],
        existing: DerivativeResult,
    ) -> DerivativeResult:
        conn = self.estate.conn
        conn.execute("BEGIN IMMEDIATE")
        try:
            current_projects = [
                row["project"]
                for row in conn.execute(
                    """SELECT project FROM document_projects
                       WHERE document_id=? ORDER BY project""",
                    (document_id,),
                )
            ]
            inserted = self._insert_parsed_event_locked(
                document_id,
                derivation_id,
                current_projects or projects,
                now=_now(),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        if not inserted:
            return existing
        return DerivativeResult(
            status="succeeded",
            document_id=document_id,
            reason="routing_reemitted",
            derivation_id=derivation_id,
            output_artifact_id=existing.output_artifact_id,
            output_sha256=existing.output_sha256,
        )

    def _insert_parsed_event_locked(
        self,
        document_id: str,
        derivation_id: str,
        projects: Sequence[str],
        *,
        now: str,
    ) -> bool:
        row = self.estate.conn.execute(
            """SELECT doc.company,doc.doc_type,doc.period,
                      d.processor_name,d.processor_version,
                      d.input_artifact_id,d.input_sha256,
                      d.output_artifact_id,d.output_sha256,d.output_object_key,
                      ia.role AS input_role,ia.format AS input_format,
                      ia.path AS input_path,
                      oa.role AS output_role,oa.format AS output_format,
                      oa.path AS output_path,
                      ico.object_key AS input_object_key
               FROM document_derivations d
               JOIN documents doc ON doc.document_id=d.document_id
               JOIN artifacts ia ON ia.artifact_id=d.input_artifact_id
               JOIN artifacts oa ON oa.artifact_id=d.output_artifact_id
               LEFT JOIN content_objects ico ON ico.sha256=d.input_sha256
               WHERE d.derivation_id=? AND d.document_id=?""",
            (derivation_id, document_id),
        ).fetchone()
        if row is None:
            raise RuntimeError(
                f"derivation disappeared before routing: {derivation_id}"
            )
        selected_projects = sorted(
            {str(project).strip() for project in projects if str(project).strip()}
        )
        memberships = [
            {
                "company": membership["company"],
                "industry": membership["industry"],
            }
            for membership in self.estate.conn.execute(
                """SELECT company,industry FROM memberships
                   WHERE document_id=? ORDER BY company""",
                (document_id,),
            )
        ]
        routing_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "projects": selected_projects,
                    "memberships": memberships,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": 1,
            "document_id": document_id,
            "company": row["company"],
            "document_type": row["doc_type"],
            "period": row["period"],
            "projects": selected_projects,
            "memberships": memberships,
            "routing_fingerprint": routing_fingerprint,
            "derivation_id": derivation_id,
            "derivative_kind": "parsed_text",
            "processor": {
                "name": row["processor_name"],
                "version": row["processor_version"],
            },
            "input": {
                "artifact_id": row["input_artifact_id"],
                "role": row["input_role"],
                "format": row["input_format"],
                "path": row["input_path"],
                "content_sha256": row["input_sha256"],
                "object_key": row["input_object_key"],
            },
            "output": {
                "artifact_id": row["output_artifact_id"],
                "role": row["output_role"],
                "format": row["output_format"],
                "path": row["output_path"],
                "content_sha256": row["output_sha256"],
                "object_key": row["output_object_key"],
            },
        }
        cursor = self.estate.conn.execute(
            """INSERT OR IGNORE INTO outbox(
                   event_id,event_type,aggregate_id,dedupe_key,payload_json,
                   created_at,available_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                uuid.uuid4().hex,
                _PARSED_EVENT,
                document_id,
                (
                    f"{_PARSED_EVENT}:{derivation_id}:"
                    f"{routing_fingerprint}"
                ),
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                now,
            ),
        )
        return cursor.rowcount == 1

    def _artifact_id(self, ref: ArtifactRef) -> str:
        artifact_id = self.estate.artifact_id(
            ref.path,
            portable_root=self.estate_root,
        )
        if artifact_id is None:
            raise DerivativeInputError(
                f"source artifact disappeared from catalog: {ref.path}"
            )
        return artifact_id

    def _artifact_id_for_path(self, path: Path) -> str:
        artifact_id = self.estate.artifact_id(
            path,
            portable_root=self.estate_root,
        )
        if artifact_id is None:
            raise RuntimeError(f"artifact was not registered: {path}")
        return artifact_id

    def _verify_source(self, source: ArtifactRef) -> None:
        if not source.path.is_file():
            raise DerivativeInputError(f"source original is missing: {source.path}")
        actual = file_sha256(source.path)
        if actual != source.sha256:
            raise DerivativeInputError(
                f"source hash mismatch: expected {source.sha256}, got {actual}"
            )
        if source.format.lower() == "pdf":
            with source.path.open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise DerivativeInputError(
                        f"source artifact is not a PDF: {source.path}"
                    )

    def _output_path(
        self,
        company: str,
        period: str | None,
        document_id: str,
        output_sha256: str,
    ) -> Path:
        company_part = _safe_component(company, "unknown")
        base = _safe_component(period or "document", "document")
        document_key = hashlib.sha256(document_id.encode("utf-8")).hexdigest()[:12]
        return (
            self.estate_root
            / "views"
            / "parsed"
            / company_part
            / f"{base}__{document_key}__{output_sha256[:12]}.md"
        )

    def _existing_result(self, derivation_id: str) -> DerivativeResult | None:
        row = self.estate.conn.execute(
            """SELECT d.document_id,d.output_artifact_id,d.output_sha256,
                      d.output_object_key,a.path
               FROM document_derivations d
               JOIN artifacts a ON a.artifact_id=d.output_artifact_id
               WHERE d.derivation_id=?""",
            (derivation_id,),
        ).fetchone()
        if row is None:
            return None
        path = Path(row["path"])
        if not path.is_absolute():
            path = self.estate_root / path
        blob = self.estate_root / row["output_object_key"]
        if not blob.is_file() or file_sha256(blob) != row["output_sha256"]:
            raise DerivativeInputError(
                f"existing derivative blob failed verification: {blob}"
            )
        if not path.is_file():
            _link_or_copy(blob, path)
        if file_sha256(path) != row["output_sha256"]:
            raise DerivativeInputError(
                f"existing derivative path failed verification: {path}"
            )
        return DerivativeResult(
            status="skipped",
            document_id=row["document_id"],
            reason="already_derived",
            derivation_id=derivation_id,
            output_artifact_id=row["output_artifact_id"],
            output_sha256=row["output_sha256"],
        )
