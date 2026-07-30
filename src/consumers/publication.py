"""Terminal integrity verification for root-owned derivative events."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
import re
from typing import Any

from src.consumers.contracts import HandlerResult
from src.shared.document_estate import EstateReader, file_sha256


_PARSED_EVENT = "estate.document.parsed"
_FACTS_EVENT = "estate.document.facts_extracted"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXPECTED_OUTPUT = {
    _PARSED_EVENT: ("parsed_text", "parsed_text", "md"),
    _FACTS_EVENT: ("xbrl_facts", "xbrl_facts", "json"),
}


class DerivativePublicationError(RuntimeError):
    """A derivative event does not match its immutable estate state."""


def _event_value(event: object, name: str, default: object = None) -> object:
    if isinstance(event, Mapping):
        return event.get(name, default)
    return getattr(event, name, default)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DerivativePublicationError(f"{label} is required")
    return value.strip()


class DerivativePublicationVerifier:
    """Verify derivative publication state without creating another artifact.

    This consumer is deliberately always enabled by the root worker. It gives
    both derivative event types a terminal delivery receipt even when no
    optional application projection (such as Alpha Go) is enabled.
    """

    consumer_id = "root.derivative-publication-verifier.v1"
    event_types = (_PARSED_EVENT, _FACTS_EVENT)

    def __init__(self, database: str | Path, estate_root: str | Path):
        self.database = Path(database)
        self.estate_root = Path(estate_root).resolve()

    def handle(
        self,
        event: object,
        context: object | None = None,
    ) -> HandlerResult:
        del context
        event_type = str(_event_value(event, "event_type", ""))
        if event_type not in self.event_types:
            return HandlerResult.skipped("unsupported_event")
        try:
            detail = self._verify(event, event_type)
        except Exception as exc:
            return HandlerResult.dead(
                f"{type(exc).__name__}: {exc}",
                detail={"event_type": event_type},
            )
        return HandlerResult.succeeded(detail)

    def _verify(
        self,
        event: object,
        event_type: str,
    ) -> dict[str, Any]:
        payload = _event_value(event, "payload")
        if not isinstance(payload, Mapping):
            raise DerivativePublicationError("event payload must be an object")
        document_id = _required_text(
            payload.get("document_id")
            or _event_value(event, "aggregate_id"),
            "document_id",
        )
        aggregate_id = _required_text(
            _event_value(event, "aggregate_id"), "aggregate_id"
        )
        if aggregate_id != document_id:
            raise DerivativePublicationError(
                "aggregate_id does not match payload document_id"
            )
        derivation_id = _required_text(
            payload.get("derivation_id"), "derivation_id"
        )
        expected_kind, expected_role, expected_format = _EXPECTED_OUTPUT[
            event_type
        ]
        if payload.get("derivative_kind") != expected_kind:
            raise DerivativePublicationError(
                f"expected derivative_kind {expected_kind!r}"
            )

        output = payload.get("output")
        if not isinstance(output, Mapping):
            raise DerivativePublicationError("output must be an object")
        if output.get("role") != expected_role:
            raise DerivativePublicationError(
                f"expected output role {expected_role!r}"
            )
        if output.get("format") != expected_format:
            raise DerivativePublicationError(
                f"expected output format {expected_format!r}"
            )
        artifact_id = _required_text(output.get("artifact_id"), "output.artifact_id")
        output_sha256 = _required_text(
            output.get("content_sha256"), "output.content_sha256"
        )
        if _SHA256.fullmatch(output_sha256) is None:
            raise DerivativePublicationError(
                "output.content_sha256 must be a lowercase SHA-256"
            )
        object_key = _required_text(output.get("object_key"), "output.object_key")
        output_path_key, output_path = self._portable_path(
            output.get("path"), "output.path"
        )
        object_key, blob_path = self._portable_path(
            object_key, "output.object_key"
        )
        expected_object_key = (
            f"blobs/{output_sha256[:2]}/{output_sha256}"
        )
        if object_key != expected_object_key:
            raise DerivativePublicationError(
                "output.object_key is not the canonical content address"
            )
        if not output_path.is_file():
            raise DerivativePublicationError(
                f"derivative output is missing: {output_path_key}"
            )
        if file_sha256(output_path) != output_sha256:
            raise DerivativePublicationError(
                f"derivative output hash mismatch: {output_path_key}"
            )
        if not blob_path.is_file():
            raise DerivativePublicationError(
                f"derivative blob is missing: {object_key}"
            )
        if file_sha256(blob_path) != output_sha256:
            raise DerivativePublicationError(
                f"derivative blob hash mismatch: {object_key}"
            )

        with EstateReader(self.database) as reader:
            artifact = reader.conn.execute(
                """SELECT artifact_id,document_id,project,role,format,path,sha256
                   FROM artifacts WHERE artifact_id=?""",
                (artifact_id,),
            ).fetchone()
            if artifact is None:
                raise DerivativePublicationError(
                    f"output artifact is not catalogued: {artifact_id}"
                )
            expected_artifact = {
                "document_id": document_id,
                "project": "root",
                "role": expected_role,
                "format": expected_format,
                "path": output_path_key,
                "sha256": output_sha256,
            }
            for column, expected in expected_artifact.items():
                if artifact[column] != expected:
                    raise DerivativePublicationError(
                        f"artifact {column} does not match event output"
                    )

            content_object = reader.conn.execute(
                """SELECT sha256,object_key FROM content_objects
                   WHERE sha256=?""",
                (output_sha256,),
            ).fetchone()
            if content_object is None:
                raise DerivativePublicationError(
                    "output content object is not catalogued"
                )
            if content_object["object_key"] != object_key:
                raise DerivativePublicationError(
                    "content object key does not match event output"
                )

            derivation = reader.conn.execute(
                """SELECT document_id,derivative_kind,input_artifact_id,
                          input_sha256,processor_name,processor_version,
                          output_artifact_id,output_sha256,output_object_key
                   FROM document_derivations WHERE derivation_id=?""",
                (derivation_id,),
            ).fetchone()
            if derivation is None:
                raise DerivativePublicationError(
                    f"derivation is not catalogued: {derivation_id}"
                )
            expected_derivation = {
                "document_id": document_id,
                "derivative_kind": expected_kind,
                "output_artifact_id": artifact_id,
                "output_sha256": output_sha256,
                "output_object_key": object_key,
            }
            for column, expected in expected_derivation.items():
                if derivation[column] != expected:
                    raise DerivativePublicationError(
                        f"derivation {column} does not match event output"
                    )

            source = payload.get("input")
            if not isinstance(source, Mapping):
                raise DerivativePublicationError("input must be an object")
            if source.get("artifact_id") != derivation["input_artifact_id"]:
                raise DerivativePublicationError(
                    "derivation input artifact does not match event input"
                )
            if source.get("content_sha256") != derivation["input_sha256"]:
                raise DerivativePublicationError(
                    "derivation input hash does not match event input"
                )
            processor = payload.get("processor")
            if not isinstance(processor, Mapping):
                raise DerivativePublicationError("processor must be an object")
            if (
                processor.get("name") != derivation["processor_name"]
                or processor.get("version") != derivation["processor_version"]
            ):
                raise DerivativePublicationError(
                    "derivation processor does not match event processor"
                )

        return {
            "document_id": document_id,
            "derivation_id": derivation_id,
            "derivative_kind": expected_kind,
            "output_artifact_id": artifact_id,
            "output_sha256": output_sha256,
            "output_path": output_path_key,
            "output_object_key": object_key,
        }

    def _portable_path(
        self,
        value: object,
        label: str,
    ) -> tuple[str, Path]:
        raw = _required_text(value, label)
        path = Path(raw)
        if path.is_absolute():
            raise DerivativePublicationError(f"{label} must be estate-relative")
        key = path.as_posix()
        resolved = (self.estate_root / path).resolve()
        try:
            resolved.relative_to(self.estate_root)
        except ValueError as exc:
            raise DerivativePublicationError(
                f"{label} escapes the estate root"
            ) from exc
        return key, resolved


__all__ = [
    "DerivativePublicationError",
    "DerivativePublicationVerifier",
]
