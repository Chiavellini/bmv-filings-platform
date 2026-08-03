from __future__ import annotations

from dataclasses import dataclass
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from src.acquisition.writer import EstateWriter
from src.consumers.contracts import HandlerResult
from src.consumers.outbox import DeliveryLeaseLost, OutboxDispatcher


class Clock:
    def __init__(self):
        self.value = datetime(2026, 7, 28, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, seconds: int):
        self.value += timedelta(seconds=seconds)


@dataclass
class Consumer:
    consumer_id: str
    event_types: tuple[str, ...]
    outcomes: list[object]
    calls: int = 0

    def handle(self, event, context):
        self.calls += 1
        outcome = self.outcomes.pop(0) if self.outcomes else None
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "estate" / "catalog.db"
    with EstateWriter(database, tmp_path / "estate"):
        pass
    return database


def _insert_event(database: Path, *, event_id: str = "event-1") -> None:
    now = "2026-07-28T00:00:00+00:00"
    conn = sqlite3.connect(database)
    conn.execute(
        """INSERT INTO outbox(
               event_id,event_type,aggregate_id,dedupe_key,payload_json,
               created_at,available_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            event_id,
            "estate.document.stored",
            "doc-1",
            f"stored:{event_id}",
            json.dumps({"document_id": "doc-1"}),
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()


def _row(database: Path, sql: str, params=()):
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    row = conn.execute(sql, params).fetchone()
    conn.close()
    return row


def test_independent_receipts_gate_publication_and_retry(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    first = Consumer(
        "first.v1",
        ("estate.document.stored",),
        [HandlerResult.succeeded()],
    )
    second = Consumer(
        "second.v1",
        ("estate.document.stored",),
        [RuntimeError("temporary"), HandlerResult.succeeded()],
    )

    with OutboxDispatcher(
        database,
        [first, second],
        clock=clock,
        retry_base_seconds=10,
        retry_max_seconds=60,
    ) as dispatcher:
        initial = dispatcher.run(max_deliveries=10)
        assert (initial.succeeded, initial.retryable) == (1, 1)
        assert _row(
            database, "SELECT published_at FROM outbox WHERE event_id='event-1'"
        )["published_at"] is None

        clock.advance(10)
        recovered = dispatcher.run(max_deliveries=10)
        assert (recovered.succeeded, recovered.retryable) == (1, 0)

    event = _row(
        database,
        """SELECT published_at,publish_attempts,last_error
           FROM outbox WHERE event_id='event-1'""",
    )
    assert event["published_at"] is not None
    assert event["publish_attempts"] == 3
    assert event["last_error"] is None
    statuses = {
        row["consumer_id"]: row["status"]
        for row in _rows(
            database,
            """SELECT consumer_id,status FROM outbox_deliveries
               WHERE event_id='event-1'""",
        )
    }
    assert statuses == {"first.v1": "succeeded", "second.v1": "succeeded"}


def _rows(database: Path, sql: str, params=()):
    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def test_new_subscription_backfills_and_temporarily_reopens_old_event(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    first = Consumer("first.v1", ("estate.document.stored",), [None])
    with OutboxDispatcher(database, [first], clock=clock) as dispatcher:
        assert dispatcher.run().succeeded == 1
    assert _row(
        database, "SELECT published_at FROM outbox WHERE event_id='event-1'"
    )["published_at"] is not None

    second = Consumer("second.v1", ("estate.document.stored",), [None])
    with OutboxDispatcher(database, [second], clock=clock) as dispatcher:
        assert _row(
            database, "SELECT published_at FROM outbox WHERE event_id='event-1'"
        )["published_at"] is None
        assert dispatcher.run().succeeded == 1
    assert _row(
        database, "SELECT published_at FROM outbox WHERE event_id='event-1'"
    )["published_at"] is not None


def test_expired_claim_is_reclaimed_and_old_completion_is_fenced(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    consumer = Consumer("worker.v1", ("estate.document.stored",), [])
    first = OutboxDispatcher(
        database,
        [consumer],
        worker_id="worker-one",
        lease_seconds=10,
        clock=clock,
    )
    second = OutboxDispatcher(
        database,
        [consumer],
        worker_id="worker-two",
        lease_seconds=10,
        clock=clock,
    )
    try:
        stale = first.claim()
        assert stale is not None
        clock.advance(11)
        current = second.claim()
        assert current is not None
        assert current.attempt == 2
        second.complete(current, HandlerResult.succeeded())
        with pytest.raises(DeliveryLeaseLost):
            first.complete(stale, HandlerResult.succeeded())
    finally:
        first.close()
        second.close()


def test_repeated_failure_becomes_dead_and_is_not_hot_looped(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    consumer = Consumer(
        "broken.v1",
        ("estate.document.stored",),
        [RuntimeError("one"), RuntimeError("two")],
    )
    with OutboxDispatcher(
        database,
        [consumer],
        max_attempts=2,
        retry_base_seconds=5,
        retry_max_seconds=5,
        clock=clock,
    ) as dispatcher:
        first = dispatcher.run(max_deliveries=10)
        assert (first.claimed, first.retryable, consumer.calls) == (1, 1, 1)
        clock.advance(5)
        second = dispatcher.run(max_deliveries=10)
        assert (second.claimed, second.dead, consumer.calls) == (1, 1, 2)
        assert dispatcher.run(max_deliveries=10).claimed == 0

    delivery = _row(
        database,
        """SELECT status,attempt_count,last_error FROM outbox_deliveries
           WHERE event_id='event-1' AND consumer_id='broken.v1'""",
    )
    assert delivery["status"] == "dead"
    assert delivery["attempt_count"] == 2
    assert "RuntimeError: two" in delivery["last_error"]
    event = _row(
        database,
        "SELECT published_at,last_error FROM outbox WHERE event_id='event-1'",
    )
    assert event["published_at"] is not None
    assert "RuntimeError: two" in event["last_error"]


def test_status_is_read_only_and_reports_missing_schema(tmp_path):
    missing = tmp_path / "missing.db"
    assert OutboxDispatcher.read_status(missing) == {
        "initialized": False,
        "reason": "catalog_missing",
    }
    assert not missing.exists()

    database = _database(tmp_path)
    status = OutboxDispatcher.read_status(database)
    assert status["initialized"] is False
    assert "outbox_deliveries" in status["missing_tables"]


def test_trigger_enqueues_events_inserted_after_registration(tmp_path):
    database = _database(tmp_path)
    consumer = Consumer("trigger.v1", ("estate.document.stored",), [None])
    with OutboxDispatcher(database, [consumer]) as dispatcher:
        _insert_event(database)
        delivery = _row(
            database,
            """SELECT status FROM outbox_deliveries
               WHERE event_id='event-1' AND consumer_id='trigger.v1'""",
        )
        assert delivery["status"] == "pending"
        assert dispatcher.run().succeeded == 1


def test_claim_does_not_rescan_the_full_outbox(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    consumer = Consumer("bounded.v1", ("estate.document.stored",), [None])
    with OutboxDispatcher(database, [consumer]) as dispatcher:
        statements: list[str] = []
        dispatcher.conn.set_trace_callback(statements.append)
        claim = dispatcher.claim()
        dispatcher.conn.set_trace_callback(None)
        assert claim is not None
        normalized = [" ".join(statement.lower().split()) for statement in statements]
        assert not any(
            statement.startswith("insert or ignore into outbox_deliveries")
            for statement in normalized
        )
        assert not any(
            statement.startswith("update outbox set published_at=null")
            for statement in normalized
        )


def test_crashed_final_attempt_is_reaped_instead_of_reclaimed(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    consumer = Consumer("crash.v1", ("estate.document.stored",), [])
    first = OutboxDispatcher(
        database,
        [consumer],
        worker_id="first",
        max_attempts=2,
        lease_seconds=10,
        clock=clock,
    )
    second = OutboxDispatcher(
        database,
        [consumer],
        worker_id="second",
        max_attempts=2,
        lease_seconds=10,
        clock=clock,
    )
    third = OutboxDispatcher(
        database,
        [consumer],
        worker_id="third",
        max_attempts=2,
        lease_seconds=10,
        clock=clock,
    )
    try:
        assert first.claim().attempt == 1
        clock.advance(11)
        assert second.claim().attempt == 2
        clock.advance(11)
        report = third.run(max_deliveries=1)
        assert (report.claimed, report.dead, report.ok) == (0, 1, False)
        delivery = _row(
            database,
            """SELECT status,attempt_count,last_error
               FROM outbox_deliveries
               WHERE event_id='event-1' AND consumer_id='crash.v1'""",
        )
        assert delivery["status"] == "dead"
        assert delivery["attempt_count"] == 2
        assert "lease expired" in delivery["last_error"]
    finally:
        first.close()
        second.close()
        third.close()


def test_live_claim_serializes_one_consumer_but_not_another(tmp_path):
    database = _database(tmp_path)
    _insert_event(database, event_id="event-1")
    _insert_event(database, event_id="event-2")
    clock = Clock()
    serial = Consumer("serial.v1", ("estate.document.stored",), [])
    other = Consumer("other.v1", ("estate.document.stored",), [])
    first = OutboxDispatcher(
        database,
        [serial],
        worker_id="first",
        clock=clock,
    )
    same = OutboxDispatcher(
        database,
        [serial],
        worker_id="same",
        clock=clock,
    )
    independent = OutboxDispatcher(
        database,
        [other],
        worker_id="other",
        clock=clock,
    )
    try:
        assert first.claim() is not None
        assert same.claim() is None
        independent_claim = independent.claim()
        assert independent_claim is not None
        assert independent_claim.consumer_id == "other.v1"
    finally:
        first.close()
        same.close()
        independent.close()


def test_lease_renewal_is_token_fenced(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    consumer = Consumer("renew.v1", ("estate.document.stored",), [])
    with OutboxDispatcher(
        database,
        [consumer],
        lease_seconds=10,
        clock=clock,
    ) as dispatcher:
        claim = dispatcher.claim()
        assert claim is not None
        clock.advance(5)
        assert dispatcher.renew(claim) is True
        assert dispatcher.renew(replace(claim, lock_token="stale")) is False


@pytest.mark.parametrize(
    "raw_payload",
    ["{broken", "42", '{"schema_version": 2}'],
)
def test_corrupt_payload_is_dead_lettered_without_calling_handler(
    tmp_path, raw_payload
):
    database = _database(tmp_path)
    now = "2026-07-28T00:00:00+00:00"
    conn = sqlite3.connect(database)
    conn.execute(
        """INSERT INTO outbox(
               event_id,event_type,aggregate_id,dedupe_key,payload_json,
               created_at,available_at
           ) VALUES('bad','estate.document.stored','doc','bad',?,?,?)""",
        (raw_payload, now, now),
    )
    conn.commit()
    conn.close()
    consumer = Consumer("strict.v1", ("estate.document.stored",), [])
    with OutboxDispatcher(database, [consumer]) as dispatcher:
        report = dispatcher.run()
    assert (report.dead, consumer.calls) == (1, 0)
    delivery = _row(
        database,
        """SELECT status,last_error FROM outbox_deliveries
           WHERE event_id='bad' AND consumer_id='strict.v1'""",
    )
    assert delivery["status"] == "dead"
    assert "corrupt outbox payload" in delivery["last_error"]


def test_keyboard_interrupt_is_requeued_and_propagated(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    consumer = Consumer(
        "interrupt.v1",
        ("estate.document.stored",),
        [KeyboardInterrupt()],
    )
    with OutboxDispatcher(database, [consumer]) as dispatcher:
        with pytest.raises(KeyboardInterrupt):
            dispatcher.run()
    delivery = _row(
        database,
        """SELECT status,last_error FROM outbox_deliveries
           WHERE event_id='event-1' AND consumer_id='interrupt.v1'""",
    )
    assert delivery["status"] == "retryable"
    assert "KeyboardInterrupt" in delivery["last_error"]


def test_duplicate_contracts_fail_before_catalog_or_subscription_write(tmp_path):
    database = tmp_path / "new" / "catalog.db"
    first = Consumer("duplicate.v1", ("estate.document.stored",), [])
    second = Consumer("duplicate.v1", ("estate.document.stored",), [])
    with pytest.raises(ValueError, match="duplicate consumer_id"):
        OutboxDispatcher(database, [first, second])
    assert not database.exists()


def test_public_register_activates_the_in_memory_handler(tmp_path):
    database = _database(tmp_path)
    _insert_event(database)
    consumer = Consumer("late.v1", ("estate.document.stored",), [None])
    with OutboxDispatcher(database, []) as dispatcher:
        dispatcher.register(consumer)
        assert dispatcher.run().succeeded == 1
    assert consumer.calls == 1


def test_ready_downstream_consumer_is_not_starved_by_old_upstream_backlog(
    tmp_path,
):
    database = _database(tmp_path)
    for ordinal in range(50):
        _insert_event(database, event_id=f"stored-{ordinal:03d}")
    now = "2026-07-28T00:00:00+00:00"
    conn = sqlite3.connect(database)
    conn.execute(
        """INSERT INTO outbox(
               event_id,event_type,aggregate_id,dedupe_key,payload_json,
               created_at,available_at
           ) VALUES(?,?,?,?,?,?,?)""",
        (
            "parsed-new",
            "estate.document.parsed",
            "doc-new",
            "parsed-new",
            json.dumps({"document_id": "doc-new"}),
            "2026-07-29T00:00:00+00:00",
            now,
        ),
    )
    conn.commit()
    conn.close()
    derivative = Consumer(
        "root.derivative.v1",
        ("estate.document.stored",),
        [None] * 50,
    )
    alpha = Consumer(
        "alpha.projection.v1",
        ("estate.document.parsed",),
        [None],
    )
    with OutboxDispatcher(database, [derivative, alpha]) as dispatcher:
        report = dispatcher.run(max_deliveries=1)
    assert report.succeeded == 1
    assert (alpha.calls, derivative.calls) == (1, 0)


def test_dead_receipt_can_be_guardedly_replayed_with_attempt_history(
    tmp_path,
):
    database = _database(tmp_path)
    _insert_event(database)
    clock = Clock()
    broken = Consumer(
        "replay.v1",
        ("estate.document.stored",),
        [RuntimeError("one"), RuntimeError("two")],
    )
    with OutboxDispatcher(
        database,
        [broken],
        max_attempts=2,
        retry_base_seconds=1,
        retry_max_seconds=1,
        clock=clock,
    ) as dispatcher:
        assert dispatcher.run().retryable == 1
        clock.advance(1)
        assert dispatcher.run().dead == 1

    repaired = Consumer(
        "replay.v1",
        ("estate.document.stored",),
        [None],
    )
    with OutboxDispatcher(
        database,
        [repaired],
        max_attempts=2,
        clock=clock,
    ) as dispatcher:
        assert dispatcher.requeue_dead("replay.v1") == ("event-1",)
        assert dispatcher.run().succeeded == 1

    attempts = _rows(
        database,
        """SELECT replay_number,attempt_number,status
           FROM outbox_delivery_attempts
           WHERE event_id='event-1' AND consumer_id='replay.v1'
           ORDER BY replay_number,attempt_number""",
    )
    assert [
        (row["replay_number"], row["attempt_number"], row["status"])
        for row in attempts
    ] == [
        (0, 1, "retryable"),
        (0, 2, "dead"),
        (1, 1, "succeeded"),
    ]
    assert _row(
        database,
        "SELECT publish_attempts,last_error FROM outbox WHERE event_id='event-1'",
    )["publish_attempts"] == 3


def test_reregister_disables_event_types_removed_from_contract(tmp_path):
    database = _database(tmp_path)
    broad = Consumer(
        "contract.v1",
        ("estate.document.stored", "estate.document.parsed"),
        [],
    )
    with OutboxDispatcher(database, [broad]):
        pass
    narrowed = Consumer(
        "contract.v1",
        ("estate.document.stored",),
        [],
    )
    with OutboxDispatcher(database, [narrowed]):
        pass
    rows = _rows(
        database,
        """SELECT event_type,enabled FROM outbox_subscriptions
           WHERE consumer_id='contract.v1' ORDER BY event_type""",
    )
    assert [(row["event_type"], row["enabled"]) for row in rows] == [
        ("estate.document.parsed", 0),
        ("estate.document.stored", 1),
    ]


def test_managed_generation_reconciliation_disables_old_prefix_atomically(
    tmp_path,
):
    database = _database(tmp_path)
    old = Consumer(
        "root.example.v1:old",
        ("estate.document.stored",),
        [],
    )
    with OutboxDispatcher(database, [old]):
        pass

    current = Consumer(
        "root.example.v1:current",
        ("estate.document.stored",),
        [],
    )
    with OutboxDispatcher(
        database,
        [current],
        managed_generations={
            "root.example.v1:": (current.consumer_id,),
        },
    ) as dispatcher:
        assert dispatcher.disabled_consumer_ids == (old.consumer_id,)

    rows = _rows(
        database,
        """SELECT consumer_id,enabled FROM outbox_subscriptions
           WHERE consumer_id LIKE 'root.example.v1:%'
           ORDER BY consumer_id""",
    )
    assert [(row["consumer_id"], row["enabled"]) for row in rows] == [
        ("root.example.v1:current", 1),
        ("root.example.v1:old", 0),
    ]


def test_prefix_disable_is_explicit_and_can_preserve_one_generation(tmp_path):
    database = _database(tmp_path)
    first = Consumer("alpha.managed:first", ("estate.document.parsed",), [])
    second = Consumer("alpha.managed:second", ("estate.document.parsed",), [])
    with OutboxDispatcher(database, [first, second]) as dispatcher:
        assert dispatcher.disable_prefix(
            "alpha.managed:",
            keep_consumer_ids=(second.consumer_id,),
        ) == (first.consumer_id,)
        assert dispatcher.disable_prefix("alpha.managed:") == (
            second.consumer_id,
        )

    assert [
        row["enabled"]
        for row in _rows(
            database,
            """SELECT enabled FROM outbox_subscriptions
               WHERE consumer_id LIKE 'alpha.managed:%'
               ORDER BY consumer_id""",
        )
    ] == [0, 0]


def test_persisted_retry_policy_cannot_be_overwritten_by_another_worker(
    tmp_path,
):
    database = _database(tmp_path)
    consumer = Consumer("policy.v1", ("estate.document.stored",), [])
    with OutboxDispatcher(database, [consumer], max_attempts=2):
        pass
    with pytest.raises(ValueError, match="conflicts with persisted"):
        OutboxDispatcher(database, [consumer], max_attempts=3)
    assert _row(
        database,
        """SELECT max_attempts FROM outbox_subscriptions
           WHERE consumer_id='policy.v1'
             AND event_type='estate.document.stored'""",
    )["max_attempts"] == 2
