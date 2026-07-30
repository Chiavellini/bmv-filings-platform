from __future__ import annotations

import gzip
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.acquisition.models import FetchedArtifact, SourceRecord
from src.acquisition.writer import EstateWriter
from src.consumers.derivatives import (
    DerivativeInputError,
    PdfMarkdownDerivativeConsumer,
    XbrlFactsDerivativeConsumer,
)
from src.consumers.outbox import OutboxDispatcher
from src.consumers.publication import DerivativePublicationVerifier
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


def _raw_xbrl_bytes(*, revenue: float = 1250) -> bytes:
    return json.dumps(
        {
            "HechosPorIdConcepto": {
                "ifrs-full_Revenue": ["revenue"],
                "ifrs-full_ProfitLoss": ["profit"],
            },
            "HechosPorId": {
                "revenue": {
                    "EsValorNil": False,
                    "EsNumerico": True,
                    "ValorNumerico": str(revenue),
                    "IdContexto": "duration",
                    "IdUnidad": "mxn",
                    "Decimales": "0",
                },
                "profit": {
                    "EsValorNil": False,
                    "EsNumerico": True,
                    "ValorNumerico": "120",
                    "IdContexto": "duration",
                    "IdUnidad": "mxn",
                    "Decimales": "0",
                },
            },
            "ContextosPorId": {
                "duration": {
                    "Periodo": {
                        "FechaInicio": "2025-01-01T00:00:00",
                        "FechaFin": "2025-03-31T00:00:00",
                    },
                    "ValoresDimension": None,
                }
            },
            "UnidadesPorId": {
                "mxn": {
                    "Medidas": [
                        {
                            "Etiqueta": "iso4217:MXN",
                        }
                    ]
                }
            },
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")


def _stored_xbrl(
    estate_root: Path,
    *,
    compressed: bool = False,
) -> tuple[str, dict]:
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id="ACME:quarterly:2025-1T",
        issuer_slug="acme",
        document_type="regulatory_filing",
        title="ACME 2025 Q1 BMV XBRL filing",
        period_year=2025,
        period_quarter=1,
        rendition="xbrl",
        metadata={
            "adapter": "bmv_xbrl",
            "ticker": "ACME",
            "archive_ticker": "ACME",
        },
    )
    raw = _raw_xbrl_bytes()
    content = gzip.compress(raw, mtime=0) if compressed else raw
    filename = "ACME_2025-1T.json.gz" if compressed else "ACME_2025-1T.json"
    media_type = "application/gzip" if compressed else "application/json"
    with EstateWriter(estate_root / "catalog.db", estate_root) as writer:
        result = writer.store_fetched(
            FetchedArtifact(
                source,
                content,
                role="raw_xbrl",
                filename=filename,
                media_type=media_type,
            ),
            memberships=(
                {
                    "company": "acme",
                    "industry": "industrials",
                    "alpha_go": True,
                    "soft": True,
                    "earnings": True,
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


def test_xbrl_facts_derivative_is_portable_lineaged_and_idempotent(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    document_id, payload = _stored_xbrl(estate_root)
    event = _Event("estate.document.stored", document_id, payload)

    with XbrlFactsDerivativeConsumer(
        estate_root / "catalog.db",
        estate_root,
        processor_version="2026.1",
    ) as consumer:
        first = consumer.handle(event, context=object())
        repeated = consumer.handle(event, context=object())

        assert first.status == "succeeded"
        assert repeated.status == "skipped"
        assert repeated.reason == "already_derived"
        assert repeated.derivation_id == first.derivation_id

        conn = consumer.estate.conn
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM document_derivations"
            ).fetchone()[0]
            == 1
        )
        lineage = conn.execute(
            "SELECT * FROM document_derivations WHERE derivation_id=?",
            (first.derivation_id,),
        ).fetchone()
        artifact = conn.execute(
            "SELECT * FROM artifacts WHERE artifact_id=?",
            (lineage["output_artifact_id"],),
        ).fetchone()
        assert lineage["derivative_kind"] == "xbrl_facts"
        assert lineage["processor_name"] == "root.bmv_xbrl.extract_artifacts"
        assert lineage["processor_version"] == "2026.1"
        assert artifact["project"] == "root"
        assert artifact["role"] == "xbrl_facts"
        assert artifact["format"] == "json"
        assert artifact["path"] == (
            "views/reports/acme/xbrl/ACME_2025-1T_facts.json"
        )
        assert not Path(artifact["path"]).is_absolute()

        output = estate_root / artifact["path"]
        decoded = json.loads(output.read_text(encoding="utf-8"))
        assert decoded["source"] == "2025-1T.json"
        assert decoded["facts"]["ifrs-full_Revenue"][0]["value"] == 1250.0
        assert decoded["facts"]["ifrs-full_ProfitLoss"][0]["value"] == 120.0
        assert hashlib.sha256(output.read_bytes()).hexdigest() == artifact["sha256"]
        assert (
            estate_root / lineage["output_object_key"]
        ).read_bytes() == output.read_bytes()

        events = conn.execute(
            """SELECT payload_json FROM outbox
               WHERE event_type='estate.document.facts_extracted'"""
        ).fetchall()
        assert len(events) == 1
        extracted = json.loads(events[0]["payload_json"])
        assert extracted["schema_version"] == 1
        assert extracted["company"] == "acme"
        assert extracted["ticker"] == "ACME"
        assert extracted["period"] == "2025-1T"
        assert extracted["projects"] == ["alpha-go", "earnings", "soft"]
        assert extracted["derivative_kind"] == "xbrl_facts"
        assert extracted["input"]["role"] == "raw_xbrl"
        assert extracted["input"]["content_sha256"] == lineage["input_sha256"]
        assert extracted["output"]["role"] == "xbrl_facts"
        assert extracted["output"]["path"] == artifact["path"]
        assert extracted["output"]["content_sha256"] == lineage["output_sha256"]
        assert extracted["output"]["object_key"].startswith("blobs/")

    with EstateReader(estate_root / "catalog.db") as reader:
        assert reader.xbrl_facts_map("acme", project="root") == {
            "2025-1T": (
                estate_root
                / "views/reports/acme/xbrl/ACME_2025-1T_facts.json"
            )
        }


def test_xbrl_facts_derivative_accepts_gzipped_raw_json(tmp_path: Path) -> None:
    estate_root = tmp_path / "estate"
    document_id, _payload = _stored_xbrl(estate_root, compressed=True)

    with XbrlFactsDerivativeConsumer(
        estate_root / "catalog.db", estate_root
    ) as consumer:
        result = consumer.process(document_id)
        artifact = consumer.estate.conn.execute(
            "SELECT path,role FROM artifacts WHERE artifact_id=?",
            (result.output_artifact_id,),
        ).fetchone()

    assert result.status == "succeeded"
    assert artifact["role"] == "xbrl_facts"
    decoded = json.loads(
        (estate_root / artifact["path"]).read_text(encoding="utf-8")
    )
    assert decoded["source"] == "2025-1T.gz"
    assert decoded["facts"]["ifrs-full_Revenue"][0]["unit"] == "iso4217:MXN"


def test_existing_xbrl_derivative_survives_estate_relocation(tmp_path: Path) -> None:
    original_root = tmp_path / "estate-original"
    document_id, _payload = _stored_xbrl(original_root, compressed=True)
    with XbrlFactsDerivativeConsumer(
        original_root / "catalog.db", original_root
    ) as consumer:
        created = consumer.process(document_id)
        assert created.status == "succeeded"

    relocated_root = tmp_path / "estate-relocated"
    original_root.rename(relocated_root)
    with XbrlFactsDerivativeConsumer(
        relocated_root / "catalog.db", relocated_root
    ) as consumer:
        repeated = consumer.process(document_id)
        artifact = consumer.estate.conn.execute(
            "SELECT path FROM artifacts WHERE artifact_id=?",
            (repeated.output_artifact_id,),
        ).fetchone()

    assert repeated.status == "skipped"
    assert repeated.reason == "already_derived"
    assert not Path(artifact["path"]).is_absolute()
    assert (relocated_root / artifact["path"]).is_file()


def test_xbrl_facts_map_fails_closed_then_selects_corrected_current_version(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id="ACME:quarterly:2025-1T",
        issuer_slug="acme",
        document_type="regulatory_filing",
        period_year=2025,
        period_quarter=1,
        rendition="xbrl",
        metadata={"ticker": "ACME"},
    )
    with EstateWriter(database, estate_root) as writer:
        first_document = writer.store_fetched(
            FetchedArtifact(
                source,
                gzip.compress(_raw_xbrl_bytes(revenue=100), mtime=0),
                role="raw_xbrl",
                filename="ACME_2025-1T.json.gz",
                media_type="application/gzip",
            )
        ).document_id
    with XbrlFactsDerivativeConsumer(database, estate_root) as consumer:
        first = consumer.process(first_document)
        assert first.status == "succeeded"

    with EstateWriter(database, estate_root) as writer:
        second_document = writer.store_fetched(
            FetchedArtifact(
                source,
                gzip.compress(_raw_xbrl_bytes(revenue=200), mtime=0),
                role="raw_xbrl",
                filename="ACME_2025-1T.json.gz",
                media_type="application/gzip",
            )
        ).document_id
    assert second_document != first_document

    # A corrected current filing exists, but its derivative is not ready. The
    # read API must not silently serve the superseded v1 facts.
    with EstateReader(database) as reader:
        assert reader.xbrl_facts_map(
            "acme", project="root", role="xbrl_facts"
        ) == {}

    with XbrlFactsDerivativeConsumer(database, estate_root) as consumer:
        second = consumer.process(second_document)
        assert second.status == "succeeded"

    with EstateReader(database) as reader:
        facts = reader.xbrl_facts_map(
            "acme", project="root", role="xbrl_facts"
        )
        selected = reader.artifacts_for_document(
            second_document, role="xbrl_facts", fmt="json"
        )[0].path
    assert facts == {"2025-1T": selected}
    assert selected.name != "ACME_2025-1T_facts.json"
    decoded = json.loads(selected.read_text(encoding="utf-8"))
    assert decoded["facts"]["ifrs-full_Revenue"][0]["value"] == 200.0


def test_dispatcher_delivers_xbrl_to_facts_without_entering_pdf_parser(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    document_id, _payload = _stored_xbrl(estate_root, compressed=True)
    pdf = PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=lambda _path: pytest.fail("raw XBRL must not enter PDF parsing"),
    )
    facts = XbrlFactsDerivativeConsumer(database, estate_root)
    try:
        with OutboxDispatcher(database, [pdf, facts]) as dispatcher:
            report = dispatcher.run(max_deliveries=10)
            repeated = dispatcher.run(max_deliveries=10)

        assert report.claimed == 2
        assert report.succeeded == 1
        assert report.skipped == 1
        assert report.retryable == 0
        assert report.dead == 0
        assert repeated.claimed == 0
        assert facts.estate.conn.execute(
            """SELECT COUNT(*) FROM document_derivations
               WHERE document_id=? AND derivative_kind='xbrl_facts'""",
            (document_id,),
        ).fetchone()[0] == 1
        assert facts.estate.conn.execute(
            """SELECT COUNT(*) FROM outbox
               WHERE aggregate_id=?
                 AND event_type='estate.document.facts_extracted'""",
            (document_id,),
        ).fetchone()[0] == 1
    finally:
        pdf.close()
        facts.close()


def test_publication_verifier_accepts_both_root_derivative_events(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    pdf_document, _pdf_payload = _stored_pdf(estate_root)
    xbrl_document, _xbrl_payload = _stored_xbrl(estate_root, compressed=True)

    with PdfMarkdownDerivativeConsumer(
        database,
        estate_root,
        parser=lambda _path: "# ACME\n\nVerified Markdown.\n",
        parser_name="fixture",
    ) as pdf:
        assert pdf.process(pdf_document).status == "succeeded"
    with XbrlFactsDerivativeConsumer(database, estate_root) as facts:
        assert facts.process(xbrl_document).status == "succeeded"

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    events = connection.execute(
        """SELECT event_id,event_type,aggregate_id,dedupe_key,payload_json,
                  created_at,available_at
           FROM outbox
           WHERE event_type IN (
               'estate.document.parsed',
               'estate.document.facts_extracted'
           )
           ORDER BY event_type"""
    ).fetchall()
    connection.close()
    verifier = DerivativePublicationVerifier(database, estate_root)

    assert len(events) == 2
    for row in events:
        result = verifier.handle(
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "aggregate_id": row["aggregate_id"],
                "dedupe_key": row["dedupe_key"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "available_at": row["available_at"],
            }
        )
        assert result.status == "succeeded"
        assert result.detail["document_id"] == row["aggregate_id"]
        assert result.detail["output_object_key"].startswith("blobs/")


def test_publication_verifier_rejects_nonportable_or_tampered_output(
    tmp_path: Path,
) -> None:
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    document_id, _payload = _stored_xbrl(estate_root, compressed=True)
    with XbrlFactsDerivativeConsumer(database, estate_root) as facts:
        assert facts.process(document_id).status == "succeeded"
        row = facts.estate.conn.execute(
            """SELECT aggregate_id,payload_json FROM outbox
               WHERE event_type='estate.document.facts_extracted'"""
        ).fetchone()

    payload = json.loads(row["payload_json"])
    verifier = DerivativePublicationVerifier(database, estate_root)
    absolute_payload = json.loads(json.dumps(payload))
    absolute_payload["output"]["path"] = str(
        estate_root / payload["output"]["path"]
    )
    nonportable = verifier.handle(
        {
            "event_type": "estate.document.facts_extracted",
            "aggregate_id": row["aggregate_id"],
            "payload": absolute_payload,
        }
    )
    assert nonportable.status == "dead"
    assert "estate-relative" in nonportable.reason

    (estate_root / payload["output"]["path"]).write_bytes(b'{"tampered": true}')
    tampered = verifier.handle(
        {
            "event_type": "estate.document.facts_extracted",
            "aggregate_id": row["aggregate_id"],
            "payload": payload,
        }
    )
    assert tampered.status == "dead"
    assert "hash mismatch" in tampered.reason
