from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.acquisition.models import FetchedArtifact, SourceRecord
from src.acquisition.writer import EstateWriter
from src.consumers.derivatives import (
    DerivativeInputError,
    PdfMarkdownDerivativeConsumer,
)
from src.consumers.outbox import OutboxDispatcher
from src.shared.document_estate import EstateReader


def _source(*, document_type: str = "quarterly_release") -> SourceRecord:
    return SourceRecord(
        source_key="ir",
        source_record_id="acme-2025-q1",
        issuer_slug="acme",
        document_type=document_type,
        title="ACME 2025 Q1",
        period_year=2025,
        period_quarter=1,
        url="https://example.test/acme-q1.pdf",
    )


def _stored_pdf(estate_root: Path) -> tuple[str, dict]:
    with EstateWriter(estate_root / "catalog.db", estate_root) as writer:
        result = writer.store_fetched(
            FetchedArtifact(
                _source(),
                b"%PDF-1.7 synthetic source",
                filename="acme-q1.pdf",
                media_type="application/pdf",
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "industrials",
                    "alpha_go": True,
                    "soft": True,
                },
            ),
        )
        row = writer.estate.conn.execute(
            """SELECT payload_json FROM outbox
               WHERE event_type='estate.document.stored' AND aggregate_id=?""",
            (result.document_id,),
        ).fetchone()
        return result.document_id, json.loads(row["payload_json"])


@dataclass(frozen=True)
class _Event:
    event_type: str
    aggregate_id: str
    payload: dict


def test_pdf_derivative_is_content_addressed_lineaged_and_idempotent(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    document_id, payload = _stored_pdf(estate_root)
    calls: list[Path] = []

    def parser(path: Path) -> tuple[str, list]:
        calls.append(path)
        return "# ACME\n\nIngresos y pasajeros.\n", []

    event = _Event("estate.document.stored", document_id, payload)
    with PdfMarkdownDerivativeConsumer(
        estate_root / "catalog.db",
        estate_root,
        parser=parser,
        parser_name="test-parser",
        parser_version="2026.1",
    ) as consumer:
        first = consumer.handle(event, context=object())
        repeated = consumer.handle(event, context=object())

        assert first.status == "succeeded"
        assert repeated.status == "skipped"
        assert repeated.reason == "already_derived"
        assert repeated.error == "already_derived"
        assert repeated.retry_after_seconds is None
        assert repeated.derivation_id == first.derivation_id
        assert calls and len(calls) == 1

        conn = consumer.estate.conn
        lineage = conn.execute(
            "SELECT * FROM document_derivations WHERE derivation_id=?",
            (first.derivation_id,),
        ).fetchone()
        artifact = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=?",
            (lineage["output_artifact_id"],),
        ).fetchone()
        assert lineage["input_sha256"] != lineage["output_sha256"]
        assert artifact["project"] == "root"
        assert artifact["role"] == "parsed_text"
        assert artifact["format"] == "md"
        assert artifact["path"].startswith("views/parsed/acme/")
        assert not Path(artifact["path"]).is_absolute()
        output = estate_root / artifact["path"]
        assert output.read_text(encoding="utf-8") == "# ACME\n\nIngresos y pasajeros.\n"
        assert hashlib.sha256(output.read_bytes()).hexdigest() == artifact["sha256"]
        assert (
            estate_root / lineage["output_object_key"]
        ).read_bytes() == output.read_bytes()

        events = conn.execute(
            """SELECT payload_json FROM outbox
               WHERE event_type='estate.document.parsed'"""
        ).fetchall()
        assert len(events) == 1
        parsed = json.loads(events[0]["payload_json"])
        assert parsed["schema_version"] == 1
        assert parsed["projects"] == ["alpha-go", "soft"]
        assert parsed["input"]["content_sha256"] == lineage["input_sha256"]
        assert parsed["output"]["content_sha256"] == lineage["output_sha256"]
        assert parsed["output"]["object_key"].startswith("blobs/")
        assert parsed["processor"] == {
            "name": "test-parser",
            "version": "2026.1",
        }


def test_reader_document_scoped_apis_include_pins_and_metadata(tmp_path: Path) -> None:
    estate_root = tmp_path / "estate"
    document_id, _payload = _stored_pdf(estate_root)

    with EstateReader(estate_root / "catalog.db") as reader:
        document = reader.document(document_id)
        originals = reader.artifacts_for_document(
            document_id, role="original", fmt="pdf"
        )
        assert document is not None
        assert document.company == "acme"
        assert document.period == "2025-1T"
        assert document.metadata["acquisition"]["media_type"] == "application/pdf"
        assert len(originals) == 1
        assert originals[0].document_id == document_id
        assert reader.projects_for_document(document_id) == ["alpha-go", "soft"]
        assert reader.document("missing") is None
        assert reader.artifacts_for_document("missing") == []
        assert reader.projects_for_document("missing") == []


def test_tampered_source_is_rejected_before_parser_runs(tmp_path: Path) -> None:
    estate_root = tmp_path / "estate"
    document_id, _payload = _stored_pdf(estate_root)
    with EstateReader(estate_root / "catalog.db") as reader:
        source_path = reader.artifacts_for_document(
            document_id, role="original", fmt="pdf"
        )[0].path
    source_path.write_bytes(b"%PDF-1.7 tampered")
    called = False

    def parser(_path: Path) -> str:
        nonlocal called
        called = True
        return "should not run"

    with PdfMarkdownDerivativeConsumer(
        estate_root / "catalog.db", estate_root, parser=parser
    ) as consumer:
        with pytest.raises(DerivativeInputError, match="hash mismatch"):
            consumer.process(document_id)
        assert called is False
        assert (
            consumer.estate.conn.execute(
                "SELECT COUNT(*) FROM document_derivations"
            ).fetchone()[0]
            == 0
        )


def test_xbrl_document_is_explicitly_skipped(tmp_path: Path) -> None:
    estate_root = tmp_path / "estate"
    with EstateWriter(estate_root / "catalog.db", estate_root) as writer:
        result = writer.store_fetched(
            FetchedArtifact(
                _source(document_type="regulatory_filing"),
                b'{"facts": []}',
                role="raw_xbrl",
                filename="filing.json",
                media_type="application/json",
            )
        )

    with PdfMarkdownDerivativeConsumer(
        estate_root / "catalog.db",
        estate_root,
        parser=lambda _path: pytest.fail("XBRL must not enter the PDF parser"),
    ) as consumer:
        outcome = consumer.process(result.document_id)
        assert outcome.status == "skipped"
        assert outcome.reason == "xbrl_not_supported"
        assert (
            consumer.estate.conn.execute(
                "SELECT COUNT(*) FROM document_derivations"
            ).fetchone()[0]
            == 0
        )


def test_unrelated_event_is_skipped_without_catalog_lookup(tmp_path: Path) -> None:
    estate_root = tmp_path / "estate"
    with EstateWriter(estate_root / "catalog.db", estate_root):
        pass
    with PdfMarkdownDerivativeConsumer(
        estate_root / "catalog.db", estate_root, parser=lambda _path: "unused"
    ) as consumer:
        outcome = consumer.handle(
            {"event_type": "estate.other", "aggregate_id": "missing", "payload": {}}
        )
        assert outcome.status == "skipped"
        assert outcome.reason == "unsupported_event"


def test_parser_contract_version_creates_a_new_delivery_generation(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    _stored_pdf(estate_root)

    first = PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=lambda _path: "parser one",
        parser_name="fixture",
        parser_version="1",
    )
    second = PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=lambda _path: "parser two",
        parser_name="fixture",
        parser_version="2",
    )
    try:
        assert first.consumer_id != second.consumer_id
        with OutboxDispatcher(database, [first]) as dispatcher:
            assert dispatcher.run(max_deliveries=10).succeeded == 1
        with OutboxDispatcher(database, [second]) as dispatcher:
            assert dispatcher.run(max_deliveries=10).succeeded == 1
        assert second.estate.conn.execute(
            "SELECT COUNT(*) FROM document_derivations"
        ).fetchone()[0] == 2
    finally:
        first.close()
        second.close()
