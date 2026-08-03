"""Read-only onboarding freshness receipt contracts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from src.acquisition.service import (
    check_quarterly_publication_freshness,
    check_quarterly_sync_freshness,
)


NOW = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)


def _runs_database(path):
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE acquisition_runs (
               run_id TEXT PRIMARY KEY,
               scope_json TEXT NOT NULL,
               status TEXT NOT NULL,
               started_at TEXT NOT NULL,
               completed_at TEXT
           )"""
    )
    connection.commit()
    return connection


def _insert_run(
    connection,
    run_id,
    *,
    issuer="issuer",
    status="succeeded",
    completed_at=None,
    started_at=None,
    module="quarterly_acquisition",
    backfill=False,
):
    completed = completed_at or NOW - timedelta(minutes=30)
    started = started_at or NOW - timedelta(hours=1)
    connection.execute(
        "INSERT INTO acquisition_runs VALUES (?,?,?,?,?)",
        (
            run_id,
            json.dumps(
                {
                    "module": module,
                    "issuers": [issuer],
                    "wayback_backfill": backfill,
                    "strict_coverage": True,
                }
            ),
            status,
            started.isoformat() if isinstance(started, datetime) else started,
            completed.isoformat() if isinstance(completed, datetime) else completed,
        ),
    )
    connection.commit()


def _publication_schema(connection):
    connection.executescript(
        """
        CREATE TABLE documents (
            document_id TEXT PRIMARY KEY,
            company TEXT NOT NULL,
            period TEXT,
            doc_type TEXT NOT NULL
        );
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            document_id TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            sha256 TEXT NOT NULL
        );
        CREATE TABLE source_records (
            source_key TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            current_document_id TEXT,
            PRIMARY KEY(source_key, source_record_id)
        );
        CREATE TABLE source_record_versions (
            document_id TEXT PRIMARY KEY,
            source_key TEXT NOT NULL,
            source_record_id TEXT NOT NULL
        );
        CREATE TABLE acquisition_attempts (
            run_id TEXT,
            document_id TEXT,
            status TEXT NOT NULL
        );
        """
    )


def _catalog_input(
    connection,
    estate_root,
    *,
    document_id,
    artifact_id,
    relative_path,
    content,
    period="2026-2T",
    current_document_id=None,
    observed_run="fresh",
):
    path = estate_root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    source_record_id = f"record-{document_id}"
    connection.execute(
        "INSERT INTO documents VALUES (?,?,?,?)",
        (document_id, "issuer", period, "quarterly_release"),
    )
    connection.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?)",
        (
            artifact_id,
            document_id,
            relative_path.as_posix(),
            hashlib.sha256(content).hexdigest(),
        ),
    )
    connection.execute(
        "INSERT INTO source_records VALUES (?,?,?)",
        (
            "ir",
            source_record_id,
            current_document_id or document_id,
        ),
    )
    connection.execute(
        "INSERT INTO source_record_versions VALUES (?,?,?)",
        (document_id, "ir", source_record_id),
    )
    if observed_run is not None:
        connection.execute(
            "INSERT INTO acquisition_attempts VALUES (?,?,?)",
            (observed_run, document_id, "stored"),
        )
    connection.commit()
    return path


def test_freshness_fails_closed_for_missing_catalog_and_schema(tmp_path):
    missing = check_quarterly_sync_freshness(
        tmp_path / "missing.db",
        "issuer",
        timedelta(hours=2),
        now=NOW,
    )
    assert missing.fresh is False
    assert missing.reason == "catalog_missing"
    assert not (tmp_path / "missing.db").exists()

    schema_only = tmp_path / "schema-only.db"
    connection = sqlite3.connect(schema_only)
    connection.execute("CREATE TABLE documents (document_id TEXT PRIMARY KEY)")
    connection.commit()
    connection.close()

    no_runs = check_quarterly_sync_freshness(
        schema_only,
        "issuer",
        7200,
        now=NOW,
    )
    assert no_runs.fresh is False
    assert no_runs.reason == "acquisition_runs_table_missing"


def test_only_succeeded_non_backfill_scope_for_exact_issuer_is_fresh(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "partial", status="partial")
    _insert_run(connection, "other", issuer="other")
    _insert_run(connection, "backfill", backfill=True)

    absent = check_quarterly_sync_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        now=NOW,
    )
    assert absent.fresh is False
    assert absent.reason == "latest_sync_not_succeeded"
    assert absent.run_id == "partial"

    _insert_run(
        connection,
        "fresh",
        completed_at=NOW - timedelta(minutes=20),
    )
    connection.close()

    fresh = check_quarterly_sync_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        now=NOW,
    )
    assert fresh.fresh is True
    assert fresh.reason == "fresh"
    assert fresh.run_id == "fresh"
    assert fresh.age_seconds == 20 * 60


def test_newer_failed_strict_run_invalidates_older_success(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(
        connection,
        "older-success",
        status="succeeded",
        started_at=NOW - timedelta(hours=2),
        completed_at=NOW - timedelta(hours=1, minutes=30),
    )
    _insert_run(
        connection,
        "newer-failure",
        status="partial",
        started_at=NOW - timedelta(minutes=20),
        completed_at=NOW - timedelta(minutes=10),
    )
    connection.close()

    receipt = check_quarterly_sync_freshness(
        database,
        "issuer",
        timedelta(hours=4),
        now=NOW,
    )

    assert receipt.fresh is False
    assert receipt.reason == "latest_sync_not_succeeded"
    assert receipt.run_id == "newer-failure"
    assert "older success cannot prove" in (receipt.detail or "")


def test_allow_coverage_gaps_receipt_cannot_prove_freshness(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    completed = NOW - timedelta(minutes=10)
    connection.execute(
        "INSERT INTO acquisition_runs VALUES (?,?,?,?,?)",
        (
            "gap-tolerant",
            json.dumps({
                "module": "quarterly_acquisition",
                "issuers": ["issuer"],
                "wayback_backfill": False,
                "strict_coverage": False,
            }),
            "succeeded",
            (NOW - timedelta(hours=1)).isoformat(),
            completed.isoformat(),
        ),
    )
    connection.commit()
    connection.close()

    receipt = check_quarterly_sync_freshness(
        database, "issuer", timedelta(hours=2), now=NOW,
    )
    assert receipt.fresh is False
    assert receipt.reason == "no_succeeded_sync_receipt"


def test_non_finite_max_age_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError, match="finite"):
        check_quarterly_sync_freshness(tmp_path / "catalog.db", "issuer", float("nan"))


def test_stale_future_and_invalid_receipts_are_never_fresh(tmp_path):
    stale_db = tmp_path / "stale.db"
    connection = _runs_database(stale_db)
    _insert_run(
        connection,
        "stale",
        completed_at=NOW - timedelta(days=3),
    )
    connection.close()

    stale = check_quarterly_sync_freshness(
        stale_db,
        "issuer",
        timedelta(days=1),
        now=NOW,
    )
    assert stale.fresh is False
    assert stale.reason == "stale_receipt"
    assert stale.run_id == "stale"

    future_db = tmp_path / "future.db"
    connection = _runs_database(future_db)
    _insert_run(
        connection,
        "future",
        completed_at=NOW + timedelta(minutes=1),
    )
    connection.close()
    future = check_quarterly_sync_freshness(
        future_db,
        "issuer",
        timedelta(days=1),
        now=NOW,
    )
    assert future.fresh is False
    assert future.reason == "receipt_in_future"

    invalid_db = tmp_path / "invalid.db"
    connection = _runs_database(invalid_db)
    _insert_run(connection, "invalid", completed_at="not-a-timestamp")
    connection.close()
    invalid = check_quarterly_sync_freshness(
        invalid_db,
        "issuer",
        timedelta(days=1),
        now=NOW,
    )
    assert invalid.fresh is False
    assert invalid.reason == "invalid_receipt_timestamp"


def test_publication_freshness_binds_current_parsed_view_to_successful_run(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    selected = _catalog_input(
        connection,
        tmp_path,
        document_id="current",
        artifact_id="parsed-current",
        relative_path=Path("views/parsed/issuer/2026-2T__current.md"),
        content=b"current parsed report",
    )
    connection.close()

    receipt = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [selected],
        estate_root=tmp_path,
        now=NOW,
    )

    assert receipt.fresh is True
    assert receipt.reason == "fresh"
    assert receipt.input_document_ids == ("current",)
    assert receipt.input_artifact_ids == ("parsed-current",)
    assert receipt.catalog_document_ids == ("current",)
    assert receipt.run_document_ids == ("current",)
    assert len(receipt.input_watermark or "") == 64


def test_uncataloged_compatibility_view_binds_by_hash_but_local_copy_does_not(
    tmp_path,
):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    original = _catalog_input(
        connection,
        tmp_path,
        document_id="current",
        artifact_id="original-current",
        relative_path=Path("objects/current.md"),
        content=b"catalogued source materialized into a compatibility view",
    )
    trusted_view = tmp_path / "views/reports/issuer/2026-2T.md"
    trusted_view.parent.mkdir(parents=True)
    trusted_view.write_bytes(original.read_bytes())
    local_copy = tmp_path / "data/reports/issuer/2026-2T.md"
    local_copy.parent.mkdir(parents=True)
    local_copy.write_bytes(original.read_bytes())
    connection.close()

    trusted = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [trusted_view],
        estate_root=tmp_path,
        now=NOW,
    )
    local = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [local_copy],
        estate_root=tmp_path,
        now=NOW,
    )

    assert trusted.fresh is True
    assert trusted.input_document_ids == ("current",)
    assert trusted.input_artifact_ids == ("original-current",)
    assert local.fresh is False
    assert local.reason == "publication_input_not_cataloged"


def test_successful_sync_does_not_freshen_superseded_view_input(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    stale = _catalog_input(
        connection,
        tmp_path,
        document_id="old",
        artifact_id="parsed-old",
        relative_path=Path("views/parsed/issuer/2026-2T__old.md"),
        content=b"stale parsed report",
        current_document_id="current",
        observed_run="older-run",
    )
    connection.close()

    receipt = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [stale],
        estate_root=tmp_path,
        now=NOW,
    )

    assert receipt.fresh is False
    assert receipt.reason == "publication_input_superseded"


def test_successful_sync_does_not_freshen_untracked_local_cache(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    canonical = _catalog_input(
        connection,
        tmp_path,
        document_id="current",
        artifact_id="parsed-current",
        relative_path=Path("views/parsed/issuer/2026-2T__current.md"),
        content=b"same bytes do not imply lineage",
    )
    local = tmp_path / "data/reports/issuer/2026-2T.md"
    local.parent.mkdir(parents=True)
    local.write_bytes(canonical.read_bytes())
    connection.close()

    receipt = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [local],
        estate_root=tmp_path,
        now=NOW,
    )

    assert receipt.fresh is False
    assert receipt.reason == "publication_input_not_cataloged"


def test_successful_sync_does_not_freshen_view_missing_newest_catalog_period(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    selected = _catalog_input(
        connection,
        tmp_path,
        document_id="q1-current",
        artifact_id="parsed-q1",
        relative_path=Path("views/parsed/issuer/2026-1T__current.md"),
        content=b"first quarter",
        period="2026-1T",
    )
    _catalog_input(
        connection,
        tmp_path,
        document_id="q2-current",
        artifact_id="parsed-q2",
        relative_path=Path("views/parsed/issuer/2026-2T__current.md"),
        content=b"second quarter is catalog current but absent from the view selection",
        period="2026-2T",
    )
    connection.close()

    receipt = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [selected],
        estate_root=tmp_path,
        now=NOW,
    )

    assert receipt.fresh is False
    assert receipt.reason == "publication_view_behind_catalog"
    assert receipt.input_document_ids == ("q1-current",)
    assert receipt.catalog_document_ids == ("q2-current",)


def test_current_view_without_latest_run_observation_is_not_publication_fresh(tmp_path):
    database = tmp_path / "catalog.db"
    connection = _runs_database(database)
    _insert_run(connection, "fresh")
    _publication_schema(connection)
    selected = _catalog_input(
        connection,
        tmp_path,
        document_id="current",
        artifact_id="parsed-current",
        relative_path=Path("views/parsed/issuer/2026-2T__current.md"),
        content=b"current but not re-observed",
        observed_run="older-run",
    )
    connection.close()

    receipt = check_quarterly_publication_freshness(
        database,
        "issuer",
        timedelta(hours=2),
        [selected],
        estate_root=tmp_path,
        now=NOW,
    )

    assert receipt.fresh is False
    assert receipt.reason == "publication_watermark_not_observed"
    assert receipt.input_document_ids == ("current",)
