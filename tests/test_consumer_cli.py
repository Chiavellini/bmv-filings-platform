from __future__ import annotations

import json
from pathlib import Path
import sqlite3

import pytest

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
