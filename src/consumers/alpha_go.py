"""Narrow subprocess boundary from estate events to Alpha Go search.

Root workers never import Alpha Go's application ``src`` package.  The handler
invokes the app-owned projection CLI with one immutable estate document ID; the
Alpha side resolves that event to the current document-family version and
performs the index replacement transaction.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, Literal


_PARSED_EVENT = "estate.document.parsed"


@dataclass(frozen=True, slots=True)
class ProjectionResult:
    status: Literal["succeeded", "skipped"]
    document_id: str | None
    reason: str | None = None
    detail: Mapping[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    @property
    def skipped(self) -> bool:
        return self.status == "skipped"

    @property
    def retry_after_seconds(self) -> None:
        return None


def _value(event: object, name: str, default: object = None) -> object:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _payload(event: object) -> dict[str, Any]:
    raw = _value(event, "payload")
    if raw is None:
        raw = _value(event, "payload_json")
    if raw is None:
        return {}
    if isinstance(raw, str):
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("outbox payload must be a JSON object")
        return parsed
    if isinstance(raw, Mapping):
        return dict(raw)
    raise TypeError("outbox payload must be a mapping or JSON object string")


class AlphaGoProjectionConsumer:
    """Project parsed estate documents through Alpha Go's owned CLI."""

    event_types = (_PARSED_EVENT,)
    batch_size = 64

    def __init__(
        self,
        database: str | Path,
        *,
        project_root: str | Path,
        corpus: str | Path,
        index_db: str | Path,
        config: str | Path,
        target_id: str | None = None,
        python_executable: str | Path = sys.executable,
        timeout_seconds: int = 1800,
        runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ):
        self.database = Path(database).resolve()
        self.project_root = Path(project_root).resolve()
        self.corpus = Path(corpus).resolve()
        self.index_db = Path(index_db).resolve()
        self.config = Path(config).resolve()
        target_identity = (
            str(target_id).strip()
            if target_id is not None
            else f"{self.corpus}\0{self.index_db}"
        )
        if not target_identity:
            raise ValueError("target_id cannot be empty")
        self.target_id = target_identity
        target_digest = hashlib.sha256(
            target_identity.encode("utf-8")
        ).hexdigest()[:16]
        self.consumer_id = (
            f"alpha-go.search-projection.v1:{target_digest}"
        )
        self.python_executable = str(python_executable)
        self.timeout_seconds = int(timeout_seconds)
        self.runner = runner
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    def _current_projects(self, document_id: str) -> set[str]:
        connection = sqlite3.connect(
            f"file:{self.database}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            table = connection.execute(
                """SELECT 1 FROM sqlite_master
                   WHERE type='table' AND name='document_projects'"""
            ).fetchone()
            if table is None:
                raise RuntimeError(
                    "estate catalog has no authoritative document_projects table"
                )
            return {
                str(row["project"])
                for row in connection.execute(
                    """SELECT project FROM document_projects
                       WHERE document_id=?""",
                    (document_id,),
                )
            }
        finally:
            connection.close()

    def handle(
        self, event: object, context: object | None = None
    ) -> ProjectionResult:
        del context
        event_type = str(_value(event, "event_type", ""))
        payload = _payload(event)
        document_id = payload.get("document_id") or _value(
            event, "aggregate_id"
        )
        if event_type != _PARSED_EVENT:
            return ProjectionResult(
                "skipped",
                str(document_id) if document_id else None,
                reason="unsupported_event",
            )
        if not document_id:
            raise ValueError("parsed-document event has no document_id")
        if "alpha-go" not in self._current_projects(str(document_id)):
            return ProjectionResult(
                "skipped", str(document_id), reason="project_not_pinned"
            )
        detail = self._project((str(document_id),))
        return ProjectionResult(
            "succeeded",
            str(document_id),
            detail=detail,
        )

    def handle_batch(
        self,
        events: Sequence[object],
        contexts: Sequence[object] | None = None,
    ) -> list[ProjectionResult]:
        del contexts
        outcomes: list[ProjectionResult | None] = []
        projected: list[tuple[int, str]] = []
        for event in events:
            event_type = str(_value(event, "event_type", ""))
            payload = _payload(event)
            document_id = payload.get("document_id") or _value(
                event, "aggregate_id"
            )
            if event_type != _PARSED_EVENT:
                outcomes.append(
                    ProjectionResult(
                        "skipped",
                        str(document_id) if document_id else None,
                        reason="unsupported_event",
                    )
                )
                continue
            if not document_id:
                raise ValueError("parsed-document event has no document_id")
            document_id = str(document_id)
            if "alpha-go" not in self._current_projects(document_id):
                outcomes.append(
                    ProjectionResult(
                        "skipped", document_id, reason="project_not_pinned"
                    )
                )
                continue
            projected.append((len(outcomes), document_id))
            outcomes.append(None)
        if projected:
            detail = self._project(
                tuple(dict.fromkeys(document_id for _index, document_id in projected))
            )
            for index, document_id in projected:
                outcomes[index] = ProjectionResult(
                    "succeeded",
                    document_id,
                    detail=detail,
                )
        return [
            outcome
            for outcome in outcomes
            if outcome is not None
        ]

    def _project(self, document_ids: Sequence[str]) -> dict[str, Any]:
        script = self.project_root / "scripts" / "sync_shared_estate.py"
        for path, label in (
            (script, "Alpha Go projection script"),
            (self.config, "Alpha Go runtime config"),
            (self.database, "estate catalog"),
        ):
            if not path.is_file():
                raise FileNotFoundError(f"{label} not found: {path}")

        command = [
            self.python_executable,
            str(script),
            "--estate",
            str(self.database),
            "--corpus",
            str(self.corpus),
            "--db",
            str(self.index_db),
            "--config",
            str(self.config),
            "--apply",
            "--json",
        ]
        for document_id in document_ids:
            command.extend(("--document-id", str(document_id)))
        completed = self.runner(
            command,
            cwd=self.project_root,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        if completed.returncode != 0:
            stderr = (completed.stderr or "").strip()
            stdout = (completed.stdout or "").strip()
            diagnostic = stderr or stdout or "no subprocess output"
            raise RuntimeError(
                "Alpha Go projection failed "
                f"(exit {completed.returncode}): {diagnostic[-2000:]}"
            )
        try:
            response = json.loads((completed.stdout or "").strip())
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                "Alpha Go projection returned invalid machine output"
            ) from exc
        result = response.get("result")
        if (
            response.get("status") != "succeeded"
            or not isinstance(result, Mapping)
            or int(result.get("eligible") or 0) < 1
        ):
            raise RuntimeError(
                "Alpha Go projection did not verify the requested document: "
                f"{response}"
            )
        return {
            "command": "sync_shared_estate",
            "target_id": self.target_id,
            "requested_documents": len(document_ids),
            "result": dict(result),
        }


__all__ = ["AlphaGoProjectionConsumer", "ProjectionResult"]
