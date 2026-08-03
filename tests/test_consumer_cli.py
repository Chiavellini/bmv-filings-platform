from __future__ import annotations

import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from src.acquisition.models import FetchedArtifact, SourceRecord
from src.acquisition.writer import EstateWriter
from src.consumers.alpha_go import AlphaGoProjectionConsumer
from src.consumers.cli import main


def test_status_is_read_only_when_catalog_is_missing(tmp_path, capsys):
    database = tmp_path / "estate" / "catalog.db"

    assert main(["status", "--database", str(database), "--json"]) == 1

    assert not database.exists()
    assert json.loads(capsys.readouterr().out) == {
        "initialized": False,
        "reason": "catalog_missing",
    }


def test_run_requires_explicit_apply_before_creating_catalog(tmp_path):
    database = tmp_path / "estate" / "catalog.db"

    with pytest.raises(SystemExit) as stopped:
        main(["run", "--database", str(database)])

    assert stopped.value.code == 2
    assert not database.exists()


def test_requeue_requires_explicit_apply_before_creating_catalog(tmp_path):
    database = tmp_path / "estate" / "catalog.db"

    with pytest.raises(SystemExit) as stopped:
        main(
            [
                "requeue",
                "--database",
                str(database),
                "--consumer-id",
                "consumer.v1",
            ]
        )

    assert stopped.value.code == 2
    assert not database.exists()


def test_apply_can_initialize_an_empty_bounded_worker_catalog(tmp_path):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"

    assert (
        main(
            [
                "run",
                "--apply",
                "--estate-root",
                str(estate_root),
                "--max-deliveries",
                "0",
                "--json",
            ]
        )
        == 0
    )

    conn = sqlite3.connect(database)
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()
    assert {"outbox_subscriptions", "outbox_deliveries"} <= tables


def test_run_enables_the_root_xbrl_facts_consumer_by_default(tmp_path, capsys):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    raw = json.dumps(
        {
            "HechosPorIdConcepto": {
                "ifrs-full_Revenue": ["revenue"],
            },
            "HechosPorId": {
                "revenue": {
                    "EsValorNumerico": True,
                    "EsNumerico": True,
                    "ValorNumerico": "42",
                    "IdContexto": "quarter",
                    "IdUnidad": "mxn",
                }
            },
            "ContextosPorId": {
                "quarter": {
                    "Periodo": {
                        "FechaInicio": "2025-01-01",
                        "FechaFin": "2025-03-31",
                    }
                }
            },
            "UnidadesPorId": {
                "mxn": {"Medidas": [{"Etiqueta": "iso4217:MXN"}]}
            },
        }
    ).encode("utf-8")
    source = SourceRecord(
        source_key="bmv-xbrl",
        source_record_id="ACME:quarterly:2025-1T",
        issuer_slug="acme",
        document_type="regulatory_filing",
        period_year=2025,
        period_quarter=1,
        metadata={"ticker": "ACME"},
    )
    with EstateWriter(database, estate_root) as writer:
        document_id = writer.store_fetched(
            FetchedArtifact(
                source,
                gzip.compress(raw, mtime=0),
                role="raw_xbrl",
                filename="ACME_2025-1T.json.gz",
                media_type="application/gzip",
            )
        ).document_id

    assert (
        main(
            [
                "run",
                "--apply",
                "--estate-root",
                str(estate_root),
                "--max-deliveries",
                "10",
                "--json",
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["claimed"] == 3
    assert report["succeeded"] == 2
    assert report["skipped"] == 1
    assert report["disabled_consumer_ids"] == []

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    artifact = conn.execute(
        """SELECT project,role,format,path FROM artifacts
           WHERE document_id=? AND role='xbrl_facts'""",
        (document_id,),
    ).fetchone()
    published = conn.execute(
        """SELECT published_at FROM outbox
           WHERE aggregate_id=?
             AND event_type='estate.document.facts_extracted'""",
        (document_id,),
    ).fetchone()
    verification = conn.execute(
        """SELECT d.status FROM outbox_deliveries d
           JOIN outbox o ON o.event_id=d.event_id
           WHERE o.aggregate_id=?
             AND o.event_type='estate.document.facts_extracted'
             AND d.consumer_id='root.derivative-publication-verifier.v1'""",
        (document_id,),
    ).fetchone()
    conn.close()
    assert dict(artifact) == {
        "project": "root",
        "role": "xbrl_facts",
        "format": "json",
        "path": "views/reports/acme/xbrl/ACME_2025-1T_facts.json",
    }
    assert published["published_at"]
    assert verification["status"] == "succeeded"
    assert (
        main(["status", "--estate-root", str(estate_root), "--json"])
        == 0
    )


def test_managed_root_generation_bumps_disable_previous_consumers(
    tmp_path,
    capsys,
):
    estate_root = tmp_path / "estate"
    common = [
        "run",
        "--apply",
        "--estate-root",
        str(estate_root),
        "--max-deliveries",
        "0",
        "--json",
    ]
    assert main(common) == 0
    capsys.readouterr()
    assert main(
        common
        + [
            "--parser-version",
            "2",
            "--xbrl-processor-version",
            "2",
        ]
    ) == 0
    report = json.loads(capsys.readouterr().out)

    conn = sqlite3.connect(estate_root / "catalog.db")
    rows = conn.execute(
        """SELECT consumer_id,MIN(enabled),MAX(enabled)
           FROM outbox_subscriptions
           WHERE consumer_id LIKE 'root.pdf-markdown.v1:%'
              OR consumer_id LIKE 'root.xbrl-facts.v1:%'
           GROUP BY consumer_id ORDER BY consumer_id"""
    ).fetchall()
    conn.close()
    assert len(rows) == 4
    assert sorted(row[1] for row in rows) == [0, 0, 1, 1]
    assert sorted(row[2] for row in rows) == [0, 0, 1, 1]
    assert len(report["disabled_consumer_ids"]) == 2
    assert any(
        value.startswith("root.pdf-markdown.v1:")
        for value in report["disabled_consumer_ids"]
    )
    assert any(
        value.startswith("root.xbrl-facts.v1:")
        for value in report["disabled_consumer_ids"]
    )


def test_alpha_generations_are_reconciled_only_when_explicitly_requested(
    tmp_path,
    capsys,
):
    estate_root = tmp_path / "estate"
    alpha_index = tmp_path / "alpha.db"

    def run(*extra: str) -> dict:
        assert (
            main(
                [
                    "run",
                    "--apply",
                    "--estate-root",
                    str(estate_root),
                    "--max-deliveries",
                    "0",
                    "--json",
                    *extra,
                ]
            )
            == 0
        )
        return json.loads(capsys.readouterr().out)

    run(
        "--enable-alpha-go",
        "--alpha-index",
        str(alpha_index),
        "--alpha-target-id",
        "generation-one",
    )
    second = run(
        "--enable-alpha-go",
        "--alpha-index",
        str(alpha_index),
        "--alpha-target-id",
        "generation-two",
    )
    assert len(
        [
            value
            for value in second["disabled_consumer_ids"]
            if value.startswith("alpha-go.search-projection.v2:")
        ]
    ) == 1

    conn = sqlite3.connect(estate_root / "catalog.db")
    assert conn.execute(
        """SELECT COUNT(DISTINCT consumer_id) FROM outbox_subscriptions
           WHERE enabled=1
             AND consumer_id LIKE 'alpha-go.search-projection.v2:%'"""
    ).fetchone()[0] == 1
    conn.close()

    # A root-only worker must preserve the explicitly selected Alpha target.
    run()
    conn = sqlite3.connect(estate_root / "catalog.db")
    assert conn.execute(
        """SELECT COUNT(DISTINCT consumer_id) FROM outbox_subscriptions
           WHERE enabled=1
             AND consumer_id LIKE 'alpha-go.search-projection.v2:%'"""
    ).fetchone()[0] == 1
    conn.close()

    disabled = run("--disable-alpha-go")
    assert any(
        value.startswith("alpha-go.search-projection.v2:")
        for value in disabled["disabled_consumer_ids"]
    )
    conn = sqlite3.connect(estate_root / "catalog.db")
    assert conn.execute(
        """SELECT COUNT(*) FROM outbox_subscriptions
           WHERE enabled=1
             AND consumer_id LIKE 'alpha-go.search-projection.v2:%'"""
    ).fetchone()[0] == 0
    conn.close()


def test_status_fails_for_future_retryable_and_terminal_dead_receipts(
    tmp_path,
    capsys,
):
    estate_root = tmp_path / "estate"
    assert (
        main(
            [
                "run",
                "--apply",
                "--estate-root",
                str(estate_root),
                "--max-deliveries",
                "0",
                "--json",
            ]
        )
        == 0
    )
    capsys.readouterr()
    database = estate_root / "catalog.db"
    conn = sqlite3.connect(database)
    now = "2026-07-30T00:00:00+00:00"
    conn.execute(
        """INSERT INTO outbox(
               event_id,event_type,aggregate_id,dedupe_key,payload_json,
               created_at,available_at
           ) VALUES('future','estate.document.stored','doc','future','{}',?,?)""",
        (now, now),
    )
    conn.execute(
        """UPDATE outbox_deliveries
           SET status='retryable',available_at='2099-01-01T00:00:00+00:00'
           WHERE event_id='future'"""
    )
    conn.commit()
    conn.close()

    assert main(["status", "--database", str(database), "--json"]) == 1
    future = json.loads(capsys.readouterr().out)
    assert future["ready_receipts"] == 0
    assert future["deliveries"]["retryable"] == 2

    conn = sqlite3.connect(database)
    conn.execute(
        """UPDATE outbox_deliveries SET status='dead',completed_at=?
           WHERE event_id='future'""",
        (now,),
    )
    conn.execute(
        "UPDATE outbox SET published_at=? WHERE event_id='future'",
        (now,),
    )
    conn.commit()
    conn.close()
    assert main(["status", "--database", str(database), "--json"]) == 1
    dead = json.loads(capsys.readouterr().out)
    assert dead["unpublished_events"] == 0
    assert dead["deliveries"]["dead"] == 2


def test_require_drained_fails_closed_when_bounded_run_leaves_receipts(
    tmp_path,
    capsys,
):
    estate_root = tmp_path / "estate"
    database = estate_root / "catalog.db"
    assert main(
        [
            "run",
            "--apply",
            "--estate-root",
            str(estate_root),
            "--max-deliveries",
            "0",
            "--json",
        ]
    ) == 0
    capsys.readouterr()
    connection = sqlite3.connect(database)
    now = "2026-08-01T00:00:00+00:00"
    connection.execute(
        """INSERT INTO outbox(
               event_id,event_type,aggregate_id,dedupe_key,payload_json,
               created_at,available_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "stored-one",
            "estate.document.stored",
            "doc-one",
            "stored:doc-one",
            json.dumps({"schema_version": 1, "document_id": "doc-one"}),
            now,
            now,
        ),
    )
    connection.commit()
    connection.close()

    exit_code = main(
        [
            "run",
            "--apply",
            "--estate-root",
            str(estate_root),
            "--max-deliveries",
            "0",
            "--require-drained",
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)

    assert exit_code == 2
    assert result["drained"] is False
    assert result["drain_blockers"]["pending"] == 2


def test_alpha_reconcile_has_explicit_apply_gate_and_machine_result(
    tmp_path,
    monkeypatch,
    capsys,
):
    estate_root = tmp_path / "estate"
    index = tmp_path / "alpha.db"
    arguments = [
        "reconcile-alpha",
        "--estate-root",
        str(estate_root),
        "--alpha-index",
        str(index),
        "--json",
    ]
    with pytest.raises(SystemExit) as stopped:
        main(arguments)
    assert stopped.value.code == 2
    assert not estate_root.exists()

    monkeypatch.setattr(
        AlphaGoProjectionConsumer,
        "reconcile",
        lambda self: {
            "command": "sync_shared_estate",
            "target_id": self.target_id,
            "requested_documents": 0,
            "result": {
                "eligible": 0,
                "changed": 0,
                "unchanged": 0,
                "indexed": 0,
                "removed": 0,
                "manifest_changed": False,
            },
        },
    )
    assert main([*arguments, "--apply"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "succeeded"
    assert result["requested_documents"] == 0


def test_alpha_audit_is_read_only_and_fails_on_projection_drift(
    tmp_path,
    monkeypatch,
    capsys,
):
    monkeypatch.setattr(
        AlphaGoProjectionConsumer,
        "audit",
        lambda self: {
            "command": "audit_shared_estate_projection",
            "target_id": self.target_id,
            "healthy": False,
            "result": {
                "eligible_families": 3,
                "indexed_eligible_documents": 2,
                "missing_from_index": 1,
            },
        },
    )
    exit_code = main(
        [
            "audit-alpha",
            "--database",
            str(tmp_path / "catalog.db"),
            "--alpha-index",
            str(tmp_path / "alpha.db"),
            "--json",
        ]
    )
    result = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert result["status"] == "drift"
    assert result["result"]["missing_from_index"] == 1
