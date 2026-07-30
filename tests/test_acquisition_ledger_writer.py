from datetime import datetime, timezone
import gzip
import json
from pathlib import Path

import pytest

from src.acquisition.ledger import (
    AcquisitionLedger,
    AcquisitionLeaseHeld,
    AcquisitionLeaseLost,
)
from src.acquisition.models import FetchedArtifact, SourceRecord
from src.acquisition.writer import EstateWriter, PayloadValidationError
from src.shared.document_estate import EstateDocument


def _record(*, record_id: str = "filing-1") -> SourceRecord:
    return SourceRecord(
        source_key="bmv:test",
        source_record_id=record_id,
        issuer_slug="acme",
        url=f"https://example.test/{record_id}.pdf",
        document_type="quarterly_release",
        title="ACME 2025 Q1",
        published_at=datetime(2025, 4, 30, tzinfo=timezone.utc),
        period_year=2025,
        period_quarter=1,
    )


def test_same_hash_is_a_noop_and_changed_hash_creates_superseding_version(tmp_path):
    database = tmp_path / "estate" / "catalog.db"
    with EstateWriter(database, tmp_path / "estate") as writer:
        source = _record()
        first = writer.store_fetched(
            FetchedArtifact(source, b"%PDF-1.7 first version", filename="release.pdf"),
            memberships=(
                {
                    "company": "acme",
                    "industry": "food",
                    "alpha_go": True,
                    "soft": True,
                    "earnings": True,
                },
            ),
        )
        repeated = writer.store_fetched(
            FetchedArtifact(source, b"%PDF-1.7 first version", filename="renamed.pdf")
        )
        changed = writer.store_fetched(
            FetchedArtifact(source, b"%PDF-1.7 corrected version", filename="release.pdf")
        )

        assert first.created is True
        assert repeated.created is False
        assert repeated.document_id == first.document_id
        assert repeated.version == 1
        assert changed.created is True
        assert changed.version == 2
        assert changed.document_id != first.document_id
        assert changed.supersedes_document_id == first.document_id
        assert first.artifact_path.read_bytes() == b"%PDF-1.7 first version"
        assert changed.artifact_path.read_bytes() == b"%PDF-1.7 corrected version"
        assert first.object_key.startswith("blobs/")
        assert not Path(first.object_key).is_absolute()
        conn = writer.estate.conn
        stored_path = conn.execute(
            "SELECT path FROM artifacts WHERE document_id=?",
            (first.document_id,),
        ).fetchone()["path"]
        assert stored_path.startswith("views/reports/")
        assert not Path(stored_path).is_absolute()

        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM source_record_versions").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 2
        assert [
            row["project"]
            for row in conn.execute(
                """SELECT project FROM document_projects
                   WHERE document_id=? ORDER BY project""",
                (first.document_id,),
            )
        ] == ["alpha-go", "earnings", "soft"]
        event = json.loads(
            conn.execute(
                "SELECT payload_json FROM outbox WHERE aggregate_id=?",
                (first.document_id,),
            ).fetchone()["payload_json"]
        )
        assert event["schema_version"] == 1
        assert event["media_type"] == "application/pdf"
        assert event["artifact_role"] == "original"
        assert event["language"] is None
        assert event["source_key"] == "acme:bmv:test"
        assert event["source_record_id"] == "filing-1"
        assert event["projects"] == ["alpha-go", "earnings", "soft"]


def test_run_lease_blocks_overlap_and_is_released(tmp_path):
    database = tmp_path / "estate.db"
    # Create the base estate tables before opening independent ledger connections.
    with EstateWriter(database, tmp_path / "estate"):
        pass
    with AcquisitionLedger(database) as first, AcquisitionLedger(database) as second:
        run_id = first.start_run(owner="worker-one")
        with pytest.raises(AcquisitionLeaseHeld, match="worker-one"):
            second.start_run(owner="worker-two")
        first.finish_run(run_id)
        replacement = second.start_run(owner="worker-two")
        second.finish_run(replacement)


def test_lease_renewal_is_fenced_and_expired_run_is_terminalized(tmp_path):
    database = tmp_path / "estate.db"
    with EstateWriter(database, tmp_path / "estate"):
        pass
    with AcquisitionLedger(database) as first, AcquisitionLedger(database) as second:
        run_id = first.start_run(owner="worker-one", lease_ttl_seconds=10)
        before = first.conn.execute(
            "SELECT expires_at FROM acquisition_leases WHERE run_id=?", (run_id,)
        ).fetchone()["expires_at"]
        after = first.renew_lease(
            run_id, owner="worker-one", lease_ttl_seconds=60
        )
        assert after > before
        with pytest.raises(AcquisitionLeaseLost, match="fenced"):
            first.renew_lease(run_id, owner="imposter")

        first.conn.execute(
            """UPDATE acquisition_leases SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE run_id=?""",
            (run_id,),
        )
        first.conn.commit()
        replacement = second.start_run(owner="worker-two")
        stale = second.conn.execute(
            "SELECT status,completed_at,error FROM acquisition_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        assert stale["status"] == "failed"
        assert stale["completed_at"]
        assert "expired" in stale["error"]
        second.finish_run(replacement)


def test_invalid_pdf_is_rejected_before_any_blob_or_catalog_write(tmp_path):
    database = tmp_path / "estate" / "catalog.db"
    with EstateWriter(database, tmp_path / "estate") as writer:
        with pytest.raises(PayloadValidationError, match="PDF magic"):
            writer.store_fetched(
                FetchedArtifact(_record(), b"<html>not a PDF</html>", filename="bad.pdf")
            )
        assert not list((tmp_path / "estate" / "blobs").rglob("*"))
        assert writer.estate.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0


def test_identical_legacy_view_is_not_adopted_from_another_document(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    legacy = estate_root / "views" / "reports" / "acme" / "2025-1T.pdf"
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(b"%PDF-1.7 shared bytes")
    with EstateWriter(database, estate_root) as writer:
        writer.estate.upsert_document(
            EstateDocument(
                "legacy-doc",
                "acme",
                "2025-1T",
                "quarterly_release",
                "Legacy",
            )
        )
        writer.estate.add_artifact(
            "legacy-doc", legacy, project="legacy", role="original"
        )
        writer.estate.commit()

        result = writer.store_fetched(
            FetchedArtifact(
                _record(),
                b"%PDF-1.7 shared bytes",
                filename="release.pdf",
            )
        )
        assert result.created
        assert result.artifact_path != legacy
        assert writer.estate.artifact_document(legacy) == "legacy-doc"
        assert (
            writer.estate.artifact_document(result.artifact_path)
            == result.document_id
        )


def test_same_local_source_identity_does_not_collide_across_issuers(tmp_path):
    database = tmp_path / "estate" / "catalog.db"
    with EstateWriter(database, tmp_path / "estate"):
        pass
    first = _record(record_id="source-check")
    second = SourceRecord(
        source_key=first.source_key,
        source_record_id=first.source_record_id,
        issuer_slug="other",
        document_type="quarterly_release",
        title="Other source check",
    )
    with AcquisitionLedger(database) as ledger:
        run_id = ledger.start_run(owner="test")
        ledger.mark_retryable(first, run_id=run_id, error="first failed")
        ledger.mark_retryable(second, run_id=run_id, error="second failed")
        rows = ledger.conn.execute(
            """SELECT company,source_key,source_record_id FROM source_records
               ORDER BY company"""
        ).fetchall()
        assert [tuple(row) for row in rows] == [
            ("acme", "acme:bmv:test", "source-check"),
            ("other", "other:bmv:test", "source-check"),
        ]
        ledger.finish_run(run_id, status="failed")


def test_regulatory_json_uses_regulatory_view_not_reports_view(tmp_path):
    estate_root = tmp_path / "estate"
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id="ACME:quarterly:2025-1T",
        issuer_slug="acme",
        document_type="regulatory_filing",
        period_year=2025,
        period_quarter=1,
    )
    with EstateWriter(estate_root / "catalog.db", estate_root) as writer:
        result = writer.store_fetched(
            FetchedArtifact(
                source,
                b'{"Revenue": 10}',
                role="raw_xbrl",
                filename="filing.json",
                media_type="application/json",
            )
        )
        assert "/views/regulatory/" in str(result.artifact_path)
        assert "/views/reports/" not in str(result.artifact_path)


@pytest.mark.parametrize(
    ("content", "filename", "media_type", "message"),
    [
        (b"not gzip", "filing.gz", "application/gzip", "gzip magic"),
        (
            gzip.compress(b"{broken"),
            "filing.json.gz",
            "application/gzip",
            "gzipped JSON",
        ),
        (b"{broken", "filing.json", "application/json", "valid UTF-8 JSON"),
    ],
)
def test_invalid_structured_payload_is_rejected_before_publish(
    tmp_path, content, filename, media_type, message
):
    estate_root = tmp_path / filename.replace(".", "_")
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id=filename,
        issuer_slug="acme",
        document_type="regulatory_filing",
        period_year=2025,
        period_quarter=1,
    )
    with EstateWriter(estate_root / "catalog.db", estate_root) as writer:
        with pytest.raises(PayloadValidationError, match=message):
            writer.store_fetched(
                FetchedArtifact(
                    source,
                    content,
                    role="raw_xbrl",
                    filename=filename,
                    media_type=media_type,
                )
            )
        assert not list((estate_root / "blobs").rglob("*"))


def test_writer_rejects_a_run_that_lost_its_fenced_lease(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    with EstateWriter(database, estate_root) as writer, AcquisitionLedger(database) as ledger:
        run_id = ledger.start_run(owner="stale-worker")
        ledger.conn.execute(
            """UPDATE acquisition_leases SET expires_at='2000-01-01T00:00:00+00:00'
               WHERE run_id=?""",
            (run_id,),
        )
        ledger.conn.commit()
        with pytest.raises(AcquisitionLeaseLost, match="live fenced lease"):
            writer.store_fetched(
                FetchedArtifact(
                    _record(),
                    b"%PDF-1.7 fenced",
                    filename="fenced.pdf",
                ),
                run_id=run_id,
            )
        assert writer.estate.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        assert writer.estate.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 0


def test_same_hash_repairs_a_missing_original_without_a_new_version(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    artifact = FetchedArtifact(
        _record(),
        b"%PDF-1.7 repairable",
        filename="repairable.pdf",
    )
    with EstateWriter(database, estate_root) as writer:
        first = writer.store_fetched(artifact)
        first.artifact_path.unlink()
        assert not first.artifact_path.exists()

        repaired = writer.store_fetched(artifact)
        assert repaired.created is False
        assert repaired.document_id == first.document_id
        assert repaired.version == 1
        assert repaired.artifact_path.is_file()
        assert repaired.artifact_path.read_bytes() == artifact.content
        assert (
            writer.estate.conn.execute(
                "SELECT COUNT(*) FROM source_record_versions"
            ).fetchone()[0]
            == 1
        )
        assert (
            writer.estate.conn.execute(
                "SELECT COUNT(*) FROM artifacts WHERE document_id=?",
                (first.document_id,),
            ).fetchone()[0]
            == 1
        )
