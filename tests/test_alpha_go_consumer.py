from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import subprocess

import pytest

from src.consumers.alpha_go import AlphaGoProjectionConsumer
from src.consumers.outbox import OutboxDispatcher


def _consumer(
    tmp_path: Path,
    calls: list,
    *,
    returncode: int = 0,
    pinned: bool = True,
    document_ids: tuple[str, ...] = ("acq:one",),
    index_name: str = "index.db",
    eligible: int = 1,
):
    project = tmp_path / "alpha-go"
    (project / "scripts").mkdir(parents=True, exist_ok=True)
    (project / "scripts" / "sync_shared_estate.py").write_text("# fixture\n")
    config = project / "runtime.yaml"
    config.write_text("index: {}\n")
    estate = tmp_path / "catalog.db"
    connection = sqlite3.connect(estate)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS document_projects(
               document_id TEXT NOT NULL,
               project TEXT NOT NULL
           )"""
    )
    if pinned:
        connection.executemany(
            "INSERT INTO document_projects VALUES(?,'alpha-go')",
            ((document_id,) for document_id in document_ids),
        )
    connection.commit()
    connection.close()

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command,
            returncode,
            stdout=(
                json.dumps(
                    {
                        "schema_version": 1,
                        "status": "succeeded",
                        "result": {
                            "eligible": eligible,
                            "changed": 1,
                            "unchanged": 0,
                            "indexed": 1,
                            "removed": 0,
                            "manifest_changed": True,
                        },
                    }
                )
                if returncode == 0
                else ""
            ),
            stderr="model mismatch" if returncode else "",
        )

    return AlphaGoProjectionConsumer(
        estate,
        project_root=project,
        corpus=tmp_path / "corpus",
        index_db=tmp_path / index_name,
        config=config,
        python_executable="/runtime/python",
        runner=runner,
    )


def test_projection_invokes_alpha_owned_narrow_command(tmp_path):
    calls = []
    consumer = _consumer(tmp_path, calls)

    result = consumer.handle(
        {
            "event_type": "estate.document.parsed",
            "aggregate_id": "acq:one",
            "payload": {
                "document_id": "acq:one",
                "projects": ["alpha-go", "soft"],
            },
        }
    )

    assert result.status == "succeeded"
    command, kwargs = calls[0]
    assert command[0] == "/runtime/python"
    assert command[-2:] == ["--document-id", "acq:one"]
    assert "--apply" in command
    assert kwargs["cwd"].name == "alpha-go"
    assert kwargs["check"] is False


def test_projection_skips_unpinned_or_unrelated_events(tmp_path):
    calls = []
    consumer = _consumer(tmp_path, calls, pinned=False)

    unpinned = consumer.handle(
        {
            "event_type": "estate.document.parsed",
            "aggregate_id": "acq:one",
            "payload": {"projects": ["soft"]},
        }
    )
    unrelated = consumer.handle(
        {"event_type": "estate.document.stored", "aggregate_id": "acq:one"}
    )

    assert (unpinned.status, unpinned.reason) == (
        "skipped",
        "project_not_pinned",
    )
    assert (unrelated.status, unrelated.reason) == (
        "skipped",
        "unsupported_event",
    )
    assert calls == []


def test_projection_failure_is_retryable_by_dispatcher(tmp_path):
    consumer = _consumer(tmp_path, [], returncode=2)

    with pytest.raises(RuntimeError, match="model mismatch"):
        consumer.handle(
            {
                "event_type": "estate.document.parsed",
                "aggregate_id": "acq:one",
                "payload": {
                    "document_id": "acq:one",
                    "projects": ["alpha-go"],
                },
            }
        )


def test_projection_rejects_success_exit_without_verified_document(tmp_path):
    consumer = _consumer(tmp_path, [], eligible=0)

    with pytest.raises(RuntimeError, match="did not verify"):
        consumer.handle(
            {
                "event_type": "estate.document.parsed",
                "aggregate_id": "acq:one",
                "payload": {"document_id": "acq:one"},
            }
        )


def test_alpha_projection_batches_receipts_into_bounded_processes(tmp_path):
    calls = []
    document_ids = tuple(f"acq:{index:03d}" for index in range(100))
    consumer = _consumer(
        tmp_path,
        calls,
        document_ids=document_ids,
    )
    database = tmp_path / "catalog.db"
    with OutboxDispatcher(database, [consumer]) as dispatcher:
        connection = sqlite3.connect(database)
        now = "2026-07-28T00:00:00+00:00"
        connection.executemany(
            """INSERT INTO outbox(
                   event_id,event_type,aggregate_id,dedupe_key,payload_json,
                   created_at,available_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                (
                    f"event-{document_id}",
                    "estate.document.parsed",
                    document_id,
                    f"parsed-{document_id}",
                    json.dumps(
                        {
                            "schema_version": 1,
                            "document_id": document_id,
                        }
                    ),
                    now,
                    now,
                )
                for document_id in document_ids
            ),
        )
        connection.commit()
        connection.close()

        report = dispatcher.run(max_deliveries=100)

    assert (report.claimed, report.succeeded) == (100, 100)
    assert len(calls) == 2
    requested_per_process = [
        command.count("--document-id") for command, _kwargs in calls
    ]
    assert requested_per_process == [64, 36]


def test_new_alpha_target_generation_backfills_historical_events(tmp_path):
    first_calls = []
    first = _consumer(
        tmp_path,
        first_calls,
        index_name="target-a.db",
    )
    database = tmp_path / "catalog.db"
    with OutboxDispatcher(database, [first]) as dispatcher:
        connection = sqlite3.connect(database)
        now = "2026-07-28T00:00:00+00:00"
        connection.execute(
            """INSERT INTO outbox(
                   event_id,event_type,aggregate_id,dedupe_key,payload_json,
                   created_at,available_at
               ) VALUES(?,?,?,?,?,?,?)""",
            (
                "parsed-one",
                "estate.document.parsed",
                "acq:one",
                "parsed-one",
                json.dumps(
                    {"schema_version": 1, "document_id": "acq:one"}
                ),
                now,
                now,
            ),
        )
        connection.commit()
        connection.close()
        assert dispatcher.run().succeeded == 1

    second_calls = []
    second = _consumer(
        tmp_path,
        second_calls,
        index_name="target-b.db",
    )
    assert first.consumer_id != second.consumer_id
    with OutboxDispatcher(database, [second]) as dispatcher:
        assert dispatcher.run().succeeded == 1
    assert (len(first_calls), len(second_calls)) == (1, 1)
