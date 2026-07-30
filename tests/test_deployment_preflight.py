from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import sqlite3

from src.deployment.preflight import (
    CheckStatus,
    PreflightMode,
    Severity,
    format_human,
    run_preflight,
)


def _registry(path: Path, *, complete: bool = True) -> Path:
    sources = [
        "        sources:",
        "          - key: bmv",
        "            kind: bmv_xbrl",
        "            xbrl_ticker: ACME",
    ]
    if complete:
        sources.extend(
            [
                "          - key: ir",
                "            kind: investor_relations",
                "            url: https://example.test/reports",
                "            live_verified_period: 2026-2T",
                "            live_verified_on: 2026-07-01",
                "            live_verified_url: https://example.test/2026-2T.pdf",
            ]
        )
    path.write_text(
        "\n".join(
            [
                "version: 1",
                "groups:",
                "  industrials:",
                "    template: industrial",
                "    issuers:",
                "      - slug: acme",
                "        ticker: ACME",
                "        name: ACME",
                "        sector: industrials",
                *sources,
                "",
            ]
        ),
        encoding="utf-8",
    )
    return path


def _catalog(
    root: Path,
    *,
    absolute_artifact: bool = False,
    dead: int = 0,
) -> Path:
    root.mkdir(parents=True)
    blob = root / "blobs" / "ab" / ("ab" * 32)
    blob.parent.mkdir(parents=True)
    blob.write_bytes(b"%PDF-1.7 fixture")
    artifact = root / "views" / "reports" / "acme.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(blob.read_bytes())
    artifact_path = str(artifact) if absolute_artifact else str(
        artifact.relative_to(root)
    )
    database = root / "catalog.db"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE documents (document_id TEXT PRIMARY KEY);
        CREATE TABLE artifacts (
            artifact_id TEXT PRIMARY KEY,
            document_id TEXT,
            role TEXT,
            sha256 TEXT,
            path TEXT
        );
        CREATE TABLE content_objects (
            sha256 TEXT PRIMARY KEY,
            blob_path TEXT,
            object_key TEXT
        );
        CREATE TABLE acquisition_schema_migrations (version INTEGER);
        CREATE TABLE acquisition_runs (run_id TEXT);
        CREATE TABLE acquisition_leases (lease_name TEXT);
        CREATE TABLE source_records (source_key TEXT);
        CREATE TABLE source_record_versions (document_id TEXT);
        CREATE TABLE document_projects (document_id TEXT);
        CREATE TABLE acquisition_attempts (attempt_id TEXT);
        CREATE TABLE outbox (
            event_id TEXT,
            event_type TEXT,
            published_at TEXT,
            available_at TEXT
        );
        CREATE TABLE outbox_subscriptions (
            consumer_id TEXT,
            event_type TEXT,
            enabled INTEGER
        );
        CREATE TABLE outbox_consumer_schema_migrations (version INTEGER);
        CREATE TABLE outbox_deliveries (
            event_id TEXT,
            consumer_id TEXT,
            status TEXT,
            attempt_count INTEGER,
            replay_count INTEGER,
            available_at TEXT,
            lock_token TEXT,
            locked_until TEXT
        );
        CREATE TABLE outbox_delivery_attempts (
            event_id TEXT,
            consumer_id TEXT,
            replay_number INTEGER,
            attempt_number INTEGER,
            lock_token TEXT,
            status TEXT
        );
        CREATE TABLE document_derivations (derivation_id TEXT);
        """
    )
    digest = "ab" * 32
    connection.execute("INSERT INTO documents VALUES ('doc-1')")
    connection.execute(
        "INSERT INTO outbox_consumer_schema_migrations VALUES (2)"
    )
    connection.execute(
        "INSERT INTO artifacts VALUES ('artifact-1','doc-1','original',?,?)",
        (digest, artifact_path),
    )
    connection.execute(
        "INSERT INTO content_objects VALUES (?,?,?)",
        (digest, str(blob), f"blobs/ab/{digest}"),
    )
    connection.execute(
        """INSERT INTO outbox VALUES(
               'event-1','estate.document.stored','2026-01-01','2026-01-01'
           )"""
    )
    connection.execute(
        """INSERT INTO outbox_subscriptions VALUES(
               'consumer.v1','estate.document.stored',1
           )"""
    )
    connection.execute(
        """INSERT INTO outbox_deliveries VALUES(
               'event-1','consumer.v1','succeeded',1,0,
               '2026-01-01',NULL,NULL
           )"""
    )
    for index in range(dead):
        connection.execute(
            """INSERT INTO outbox VALUES(
                   ?,'estate.document.stored','2026-01-01','2026-01-01'
               )""",
            (f"event-dead-{index}",),
        )
        connection.execute(
            """INSERT INTO outbox_deliveries VALUES(
                   ?,'consumer.v1','dead',1,0,'2026-01-01',NULL,NULL
               )""",
            (f"event-dead-{index}",),
        )
    connection.commit()
    connection.close()
    return database


def _by_name(report, name: str):
    return next(check for check in report.checks if check.name == name)


def test_production_preflight_passes_for_portable_complete_fixture(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    estate = tmp_path / "estate"
    database = _catalog(estate)

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 9),
        as_of=date(2026, 7, 30),
    )

    assert report.mode == PreflightMode.PRODUCTION
    assert report.exit_code == 0
    assert report.ok
    assert _by_name(report, "schema.consumer_tables").status == CheckStatus.PASS
    assert _by_name(report, "estate.absolute_artifact_paths").status == CheckStatus.PASS
    delivery = _by_name(report, "consumer.delivery_backlog")
    assert delivery.details == {
        "ready_deliveries": 0,
        "expired_running_deliveries": 0,
        "missing_delivery_receipts": 0,
        "dead_deliveries": 0,
        "statuses": {"succeeded": 1},
    }


def test_production_blocks_portability_dead_delivery_and_source_gaps(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml", complete=False)
    estate = tmp_path / "estate"
    database = _catalog(estate, absolute_artifact=True, dead=2)

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 0),
        as_of=date(2026, 7, 30),
    )

    assert report.exit_code == 1
    assert _by_name(report, "registry.source_readiness").severity == Severity.BLOCKER
    assert _by_name(report, "estate.absolute_artifact_paths").severity == Severity.BLOCKER
    delivery = _by_name(report, "consumer.delivery_backlog")
    assert delivery.severity == Severity.BLOCKER
    assert delivery.details["dead_deliveries"] == 2


def test_missing_database_is_not_created_and_audit_is_observational(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    missing = tmp_path / "estate" / "catalog.db"

    report = run_preflight(
        mode="audit",
        registry_path=registry,
        estate_root=missing.parent,
        database_path=missing,
        python_version=(3, 14, 1),
        as_of=date(2026, 7, 30),
    )

    assert report.exit_code == 0
    assert not missing.exists()
    assert _by_name(report, "estate.database").status == CheckStatus.FAIL
    assert _by_name(report, "python.supported").severity == Severity.WARNING
    assert "Deployment preflight: audit [READY]" in format_human(report)


def test_preflight_does_not_migrate_incomplete_database(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    estate = tmp_path / "estate"
    estate.mkdir()
    database = estate / "catalog.db"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE sentinel (value TEXT)")
    connection.commit()
    before = database.read_bytes()
    connection.close()

    report = run_preflight(
        mode="worker",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 4),
        as_of=date(2026, 7, 30),
    )

    assert report.exit_code == 1
    assert database.read_bytes() == before
    connection = sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    connection.close()
    assert tables == {"sentinel"}


def test_optional_alpha_paths_and_json_are_machine_readable(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    estate = tmp_path / "estate"
    database = _catalog(estate)
    index = tmp_path / "alpha.db"
    index.touch()
    corpus = tmp_path / "corpus"
    corpus.mkdir()

    report = run_preflight(
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        alpha_index_path=index,
        alpha_corpus_path=corpus,
        python_version=(3, 13, 8),
        as_of=date(2026, 7, 30),
    )
    payload = json.loads(report.to_json())

    assert payload["schema_version"] == 1
    assert payload["checks"]
    assert _by_name(report, "alpha_go.index_path").status == CheckStatus.PASS
    assert _by_name(report, "alpha_go.corpus_path").status == CheckStatus.PASS


def test_integrity_counts_missing_files_and_unregistered_original_hash(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    estate = tmp_path / "estate"
    database = _catalog(estate)
    connection = sqlite3.connect(database)
    connection.execute(
        "UPDATE artifacts SET sha256=? WHERE artifact_id='artifact-1'",
        ("cd" * 32,),
    )
    connection.execute("UPDATE content_objects SET object_key=NULL")
    paths = connection.execute(
        "SELECT path FROM artifacts UNION ALL SELECT blob_path FROM content_objects"
    ).fetchall()
    connection.commit()
    connection.close()
    for (raw_path,) in paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = estate / path
        path.unlink()

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 9),
        as_of=date(2026, 7, 30),
    )

    missing = _by_name(report, "estate.missing_files")
    assert missing.details["missing_artifacts"] == 1
    assert missing.details["missing_blobs"] == 1
    originals = _by_name(report, "estate.original_content_objects")
    assert originals.details["original_hashes_missing_from_content_objects"] == 1
    portable = _by_name(report, "estate.portable_object_keys")
    assert portable.details["missing_object_keys"] == 1
    assert report.exit_code == 1


def test_registry_rejects_enabled_primary_pdf_source_without_url(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    registry.write_text(
        registry.read_text(encoding="utf-8").replace(
            "            url: https://example.test/reports\n",
            "",
        ),
        encoding="utf-8",
    )

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=tmp_path / "estate",
        python_version=(3, 13, 9),
        as_of=date(2026, 7, 30),
    )

    load = _by_name(report, "registry.load")
    assert load.status == CheckStatus.FAIL
    assert "requires a URL" in load.message
    assert _by_name(report, "registry.source_readiness").status == (
        CheckStatus.SKIP
    )


def test_future_only_or_future_verified_source_is_not_production_ready(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    text = registry.read_text(encoding="utf-8")
    text = text.replace(
        "            url: https://example.test/reports\n",
        "            url: https://example.test/reports\n"
        "            coverage_from_period: 2027-1T\n",
    )
    text = text.replace(
        "            live_verified_period: 2026-2T",
        "            live_verified_period: 2027-1T",
    ).replace(
        "            live_verified_on: 2026-07-01",
        "            live_verified_on: 2027-04-01",
    )
    registry.write_text(text, encoding="utf-8")
    estate = tmp_path / "estate"
    database = _catalog(estate)

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 9),
        as_of=date(2026, 7, 30),
    )

    readiness = _by_name(report, "registry.source_readiness")
    assert readiness.status == CheckStatus.FAIL
    assert readiness.details["future_only_primary_pdf_count"] == 1
    assert readiness.details[
        "missing_live_verified_primary_pdf_count"
    ] == 1
    assert readiness.details["unverified_primary_pdf_binding_count"] == 1


def test_future_floor_year_source_is_not_current(tmp_path: Path) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    text = registry.read_text(encoding="utf-8").replace(
        "            url: https://example.test/reports\n",
        "            url: https://example.test/reports\n"
        "            floor_year: 2027\n",
    )
    text = text.replace(
        "            live_verified_period: 2026-2T",
        "            live_verified_period: 2027-1T",
    ).replace(
        "            live_verified_on: 2026-07-01",
        "            live_verified_on: 2027-04-01",
    )
    registry.write_text(text, encoding="utf-8")
    estate = tmp_path / "estate"
    database = _catalog(estate)

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 9),
        as_of=date(2026, 7, 30),
    )

    readiness = _by_name(report, "registry.source_readiness")
    assert readiness.details["future_only_primary_pdf_count"] == 1
    assert readiness.details[
        "stale_or_invalid_verification_binding_count"
    ] == 1


def test_live_verification_expires_after_four_quarters(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path / "issuers.yaml")
    estate = tmp_path / "estate"
    database = _catalog(estate)

    report = run_preflight(
        mode="production",
        registry_path=registry,
        estate_root=estate,
        database_path=database,
        python_version=(3, 13, 9),
        as_of=date(2030, 12, 31),
    )

    readiness = _by_name(report, "registry.source_readiness")
    assert readiness.status == CheckStatus.FAIL
    assert readiness.details[
        "missing_live_verified_primary_pdf_count"
    ] == 1
    assert readiness.details[
        "stale_or_invalid_verification_binding_count"
    ] == 1
    assert readiness.details["max_live_verification_quarter_lag"] == 4


def test_cli_alpha_paths_can_come_from_environment(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.deployment_preflight import build_parser

    index = tmp_path / "index.db"
    corpus = tmp_path / "corpus"
    monkeypatch.setenv("PDFS_ALPHA_INDEX_PATH", str(index))
    monkeypatch.setenv("PDFS_ALPHA_CORPUS_PATH", str(corpus))

    args = build_parser().parse_args([])

    assert args.alpha_index == str(index)
    assert args.alpha_corpus == str(corpus)
