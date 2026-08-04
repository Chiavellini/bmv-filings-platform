from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

from src.consumers.cli import main
from src.consumers.alpha_go import AlphaGoProjectionConsumer


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


def test_require_drained_fails_closed_when_bounded_run_leaves_receipt(
    tmp_path, capsys
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
    assert result["drain_blockers"]["pending"] == 1


def test_alpha_reconcile_has_explicit_apply_gate_and_machine_result(
    tmp_path, monkeypatch, capsys
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
    tmp_path, monkeypatch, capsys
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
