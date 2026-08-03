"""Fenced, per-consumer delivery of shared-estate outbox events.

The acquisition transaction writes one generic event.  This dispatcher creates
an independent durable receipt for every enabled consumer subscription, so one
slow or failed application cannot be mistaken for successful fleet delivery.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sqlite3
import threading
from typing import Any
import uuid

from src.acquisition.ledger import AcquisitionLedger
from src.consumers.contracts import (
    Consumer,
    DeliveryContext,
    HandlerResult,
    OutboxEvent,
)


_TERMINAL = frozenset({"succeeded", "skipped", "dead"})
_SUCCESS_TERMINAL = frozenset({"succeeded", "skipped"})
_CLAIMABLE = frozenset({"pending", "retryable", "running"})


class DeliveryLeaseLost(RuntimeError):
    """A worker tried to finish a delivery after its claim was reclaimed."""


class _LeaseHeartbeat:
    """Renew a fenced claim batch from an independent SQLite connection."""

    def __init__(
        self,
        dispatcher: "OutboxDispatcher",
        claims: Sequence["DeliveryClaim"],
    ):
        self.dispatcher = dispatcher
        self.claims = tuple(claims)
        if not self.claims:
            raise ValueError("heartbeat requires at least one claim")
        claim = self.claims[0]
        self.stopped = threading.Event()
        self.lost = False
        self.error: BaseException | None = None
        self.thread = threading.Thread(
            target=self._run,
            name=(
                "outbox-heartbeat-"
                f"{claim.consumer_id}-{claim.event.event_id[:8]}"
            ),
            daemon=True,
        )

    def start(self) -> None:
        if self.dispatcher.heartbeat_seconds is not None:
            self.thread.start()

    def stop(self) -> None:
        if self.dispatcher.heartbeat_seconds is None:
            return
        self.stopped.set()
        self.thread.join()

    def _run(self) -> None:
        interval = self.dispatcher.heartbeat_seconds
        if interval is None:
            return
        connection: sqlite3.Connection | None = None
        try:
            while not self.stopped.wait(interval):
                if connection is None:
                    connection = sqlite3.connect(self.dispatcher.database)
                    connection.execute("PRAGMA busy_timeout=5000")
                for claim in self.claims:
                    renewed = self.dispatcher._renew_with_connection(
                        connection, claim
                    )
                    if not renewed:
                        self.lost = True
                        return
        except BaseException as exc:  # surfaced before the worker commits
            self.error = exc
        finally:
            if connection is not None:
                connection.close()


@dataclass(frozen=True, slots=True)
class DeliveryClaim:
    event: OutboxEvent
    consumer_id: str
    worker_id: str
    replay_number: int
    attempt: int
    lock_token: str
    locked_until: str
    max_attempts: int

    @property
    def context(self) -> DeliveryContext:
        return DeliveryContext(
            consumer_id=self.consumer_id,
            worker_id=self.worker_id,
            attempt=self.attempt,
            lock_token=self.lock_token,
        )


@dataclass(frozen=True, slots=True)
class DeliveryFailure:
    event_id: str
    consumer_id: str
    status: str
    error: str


@dataclass(frozen=True, slots=True)
class DispatchReport:
    claimed: int = 0
    succeeded: int = 0
    skipped: int = 0
    retryable: int = 0
    dead: int = 0
    failures: tuple[DeliveryFailure, ...] = ()

    @property
    def ok(self) -> bool:
        return self.retryable == 0 and self.dead == 0

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else 1

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds")


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _consumer_contract(consumer: Consumer) -> tuple[str, tuple[str, ...]]:
    consumer_id = str(getattr(consumer, "consumer_id", "")).strip()
    raw_types = getattr(consumer, "event_types", ())
    if isinstance(raw_types, str):
        raw_types = (raw_types,)
    event_types = tuple(
        dict.fromkeys(str(value).strip() for value in raw_types if str(value).strip())
    )
    if not consumer_id:
        raise ValueError("consumer_id is required")
    if not event_types:
        raise ValueError(f"{consumer_id}: at least one event type is required")
    if not callable(getattr(consumer, "handle", None)):
        raise TypeError(f"{consumer_id}: handle(event, context) is required")
    return consumer_id, event_types


def _superseded_consumers(consumer: Consumer, consumer_id: str) -> tuple[str, ...]:
    raw = getattr(consumer, "supersedes_consumer_ids", ())
    if isinstance(raw, str):
        raw = (raw,)
    values = tuple(
        dict.fromkeys(str(value).strip() for value in raw if str(value).strip())
    )
    if consumer_id in values:
        raise ValueError(f"{consumer_id}: consumer cannot supersede itself")
    return values


def _normalize_result(value: object) -> HandlerResult:
    if value is None:
        return HandlerResult.succeeded()
    if isinstance(value, HandlerResult):
        return value
    status = str(getattr(value, "status", "")).strip().lower()
    if status not in {"succeeded", "skipped", "retryable", "dead"}:
        raise TypeError(
            "consumer result must be None, HandlerResult, or expose a valid status"
        )
    reason = getattr(value, "reason", None)
    detail = getattr(value, "detail", None)
    retry_after = getattr(value, "retry_after_seconds", None)
    if detail is None:
        detail = {
            key: item
            for key, item in (
                ("document_id", getattr(value, "document_id", None)),
                ("derivation_id", getattr(value, "derivation_id", None)),
                ("output_artifact_id", getattr(value, "output_artifact_id", None)),
                ("output_sha256", getattr(value, "output_sha256", None)),
            )
            if item is not None
        } or None
    return HandlerResult(
        status=status,
        reason=str(reason) if reason is not None else None,
        detail=detail if isinstance(detail, Mapping) else {"value": str(detail)},
        retry_after_seconds=(
            int(retry_after) if retry_after is not None else None
        ),
    )


def _decode_payload(raw: str) -> Mapping[str, Any]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"_outbox_payload_error": f"invalid JSON: {exc}"}
    if not isinstance(payload, Mapping):
        return {
            "_outbox_payload_error": (
                "payload_json must decode to a JSON object, got "
                f"{type(payload).__name__}"
            )
        }
    if payload.get("schema_version", 1) != 1:
        return {
            "_outbox_payload_error": (
                "unsupported payload schema_version: "
                f"{payload.get('schema_version')!r}"
            )
        }
    return payload


class OutboxDispatcher:
    """Deliver events once per consumer with reclaimable, fenced claims."""

    def __init__(
        self,
        database: str | Path,
        consumers: Sequence[Consumer],
        *,
        worker_id: str | None = None,
        lease_seconds: int = 900,
        max_attempts: int = 5,
        retry_base_seconds: int = 30,
        retry_max_seconds: int = 3600,
        heartbeat_seconds: float | None = None,
        clock: Callable[[], datetime] = _utc_now,
        managed_generations: Mapping[str, Sequence[str]] | None = None,
    ):
        validated: list[
            tuple[Consumer, str, tuple[str, ...], tuple[str, ...]]
        ] = []
        seen_consumer_ids: set[str] = set()
        for consumer in consumers:
            consumer_id, event_types = _consumer_contract(consumer)
            if consumer_id in seen_consumer_ids:
                raise ValueError(f"duplicate consumer_id: {consumer_id}")
            seen_consumer_ids.add(consumer_id)
            validated.append(
                (
                    consumer,
                    consumer_id,
                    event_types,
                    _superseded_consumers(consumer, consumer_id),
                )
            )
        active_ids = {consumer_id for _c, consumer_id, _e, _s in validated}
        conflicting = sorted(
            old_id
            for _consumer, _consumer_id, _event_types, superseded in validated
            for old_id in superseded
            if old_id in active_ids
        )
        if conflicting:
            raise ValueError(
                "active consumers cannot also be superseded: "
                + ", ".join(conflicting)
            )
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if max_attempts <= 0:
            raise ValueError("max_attempts must be positive")
        if retry_base_seconds <= 0 or retry_max_seconds <= 0:
            raise ValueError("retry delays must be positive")
        if retry_base_seconds > retry_max_seconds:
            raise ValueError("retry_base_seconds cannot exceed retry_max_seconds")
        if heartbeat_seconds is not None and heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.database)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self.lease_seconds = int(lease_seconds)
        self.default_max_attempts = int(max_attempts)
        self.retry_base_seconds = int(retry_base_seconds)
        self.retry_max_seconds = int(retry_max_seconds)
        self.heartbeat_seconds = (
            min(60.0, max(0.25, self.lease_seconds / 3))
            if heartbeat_seconds is None
            else float(heartbeat_seconds)
        )
        self.clock = clock
        self.consumers: dict[str, Consumer] = {}
        self._maintenance_failures: list[DeliveryFailure] = []
        managed = self._normalize_managed_generations(managed_generations)
        self._migrate()
        now = _iso(self.clock())
        self._begin()
        try:
            for _consumer, consumer_id, event_types, _superseded in validated:
                self._register_locked(
                    consumer_id,
                    event_types,
                    max_attempts=self.default_max_attempts,
                    now=now,
                )
            disabled: list[str] = []
            for _consumer, _consumer_id, _event_types, superseded in validated:
                for old_consumer_id in superseded:
                    if self._disable_locked(old_consumer_id, None, now=now):
                        disabled.append(old_consumer_id)
            for prefix, keep_consumer_ids in managed:
                disabled.extend(
                    self._disable_prefix_locked(
                        prefix,
                        keep_consumer_ids=keep_consumer_ids,
                        now=now,
                    )
                )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            self.close()
            raise
        self.disabled_consumer_ids = tuple(sorted(set(disabled)))
        self.consumers = {
            consumer_id: consumer
            for consumer, consumer_id, _event_types, _superseded in validated
        }
        self._consumer_order = tuple(sorted(self.consumers))
        self._claim_cursor = 0

    def __enter__(self) -> "OutboxDispatcher":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _migrate(self) -> None:
        # Reuse the acquisition migration so a deliberate worker start can
        # initialize the base outbox contract on an existing estate catalog.
        AcquisitionLedger(self.conn)
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS outbox_consumer_schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS outbox_subscriptions (
                consumer_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
                max_attempts INTEGER NOT NULL CHECK(max_attempts > 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(consumer_id,event_type)
            );
            CREATE TABLE IF NOT EXISTS outbox_deliveries (
                event_id TEXT NOT NULL REFERENCES outbox(event_id),
                consumer_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('pending','running','succeeded','skipped','retryable','dead')),
                attempt_count INTEGER NOT NULL DEFAULT 0,
                replay_count INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                locked_by TEXT,
                lock_token TEXT,
                locked_until TEXT,
                started_at TEXT,
                completed_at TEXT,
                last_error TEXT,
                result_json TEXT,
                PRIMARY KEY(event_id,consumer_id)
            );
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_claim
                ON outbox_deliveries(status,available_at,locked_until);
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_consumer
                ON outbox_deliveries(consumer_id,status);
            CREATE INDEX IF NOT EXISTS idx_outbox_deliveries_consumer_claim
                ON outbox_deliveries(
                    consumer_id,status,available_at,locked_until
                );
            CREATE TABLE IF NOT EXISTS outbox_delivery_attempts (
                event_id TEXT NOT NULL REFERENCES outbox(event_id),
                consumer_id TEXT NOT NULL,
                replay_number INTEGER NOT NULL,
                attempt_number INTEGER NOT NULL,
                lock_token TEXT NOT NULL,
                worker_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN (
                    'running','succeeded','skipped','retryable','dead',
                    'lease_expired'
                )),
                started_at TEXT NOT NULL,
                completed_at TEXT,
                last_error TEXT,
                result_json TEXT,
                PRIMARY KEY(
                    event_id,consumer_id,replay_number,attempt_number
                )
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_outbox_attempt_lock_token
                ON outbox_delivery_attempts(lock_token);
            CREATE INDEX IF NOT EXISTS idx_outbox_attempt_consumer
                ON outbox_delivery_attempts(consumer_id,status,started_at);
            CREATE TRIGGER IF NOT EXISTS outbox_enqueue_enabled_consumers
            AFTER INSERT ON outbox
            BEGIN
                INSERT OR IGNORE INTO outbox_deliveries(
                    event_id,consumer_id,status,attempt_count,available_at
                )
                SELECT NEW.event_id,s.consumer_id,'pending',0,NEW.available_at
                FROM outbox_subscriptions s
                WHERE s.enabled=1 AND s.event_type=NEW.event_type;
            END;
            """
        )
        delivery_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(outbox_deliveries)"
            )
        }
        if "replay_count" not in delivery_columns:
            self.conn.execute(
                """ALTER TABLE outbox_deliveries
                   ADD COLUMN replay_count INTEGER NOT NULL DEFAULT 0"""
            )
        attempt_columns = {
            row["name"]
            for row in self.conn.execute(
                "PRAGMA table_info(outbox_delivery_attempts)"
            )
        }
        if "replay_number" not in attempt_columns:
            self.conn.executescript(
                """
                ALTER TABLE outbox_delivery_attempts
                    RENAME TO outbox_delivery_attempts_legacy;
                CREATE TABLE outbox_delivery_attempts (
                    event_id TEXT NOT NULL REFERENCES outbox(event_id),
                    consumer_id TEXT NOT NULL,
                    replay_number INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    lock_token TEXT NOT NULL,
                    worker_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN (
                        'running','succeeded','skipped','retryable','dead',
                        'lease_expired'
                    )),
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    last_error TEXT,
                    result_json TEXT,
                    PRIMARY KEY(
                        event_id,consumer_id,replay_number,attempt_number
                    )
                );
                INSERT INTO outbox_delivery_attempts(
                    event_id,consumer_id,replay_number,attempt_number,
                    lock_token,worker_id,status,started_at,completed_at,
                    last_error,result_json
                )
                SELECT event_id,consumer_id,0,attempt_number,lock_token,
                       worker_id,status,started_at,completed_at,last_error,
                       result_json
                FROM outbox_delivery_attempts_legacy;
                DROP TABLE outbox_delivery_attempts_legacy;
                CREATE UNIQUE INDEX idx_outbox_attempt_lock_token
                    ON outbox_delivery_attempts(lock_token);
                CREATE INDEX idx_outbox_attempt_consumer
                    ON outbox_delivery_attempts(
                        consumer_id,status,started_at
                    );
                """
            )
        now = _iso(self.clock())
        self.conn.execute(
            """INSERT OR IGNORE INTO outbox_consumer_schema_migrations(
                   version,applied_at
               ) VALUES(1,?)""",
            (now,),
        )
        self.conn.execute(
            """INSERT OR IGNORE INTO outbox_consumer_schema_migrations(
                   version,applied_at
               ) VALUES(2,?)""",
            (now,),
        )
        self.conn.commit()

    def _begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    @staticmethod
    def _normalize_managed_generations(
        managed: Mapping[str, Sequence[str]] | None,
    ) -> tuple[tuple[str, tuple[str, ...]], ...]:
        normalized: list[tuple[str, tuple[str, ...]]] = []
        for raw_prefix, raw_keep in (managed or {}).items():
            prefix = str(raw_prefix).strip()
            if not prefix:
                raise ValueError("managed consumer prefix cannot be empty")
            keep = tuple(
                sorted(
                    {
                        str(consumer_id).strip()
                        for consumer_id in raw_keep
                        if str(consumer_id).strip()
                    }
                )
            )
            if any(not consumer_id.startswith(prefix) for consumer_id in keep):
                raise ValueError(
                    f"managed consumer IDs must start with prefix {prefix!r}"
                )
            normalized.append((prefix, keep))
        prefixes = [prefix for prefix, _keep in normalized]
        if any(
            left != right
            and (left.startswith(right) or right.startswith(left))
            for left in prefixes
            for right in prefixes
        ):
            raise ValueError("managed consumer prefixes cannot overlap")
        return tuple(sorted(normalized))

    def _disable_prefix_locked(
        self,
        prefix: str,
        *,
        keep_consumer_ids: Sequence[str],
        now: str,
    ) -> tuple[str, ...]:
        clauses = [
            "enabled=1",
            "substr(consumer_id,1,?)=?",
        ]
        params: list[object] = [len(prefix), prefix]
        keep = tuple(dict.fromkeys(keep_consumer_ids))
        if keep:
            placeholders = ",".join("?" for _ in keep)
            clauses.append(f"consumer_id NOT IN ({placeholders})")
            params.extend(keep)
        rows = self.conn.execute(
            f"""SELECT DISTINCT consumer_id FROM outbox_subscriptions
                WHERE {' AND '.join(clauses)}
                ORDER BY consumer_id""",
            params,
        ).fetchall()
        consumer_ids = tuple(row["consumer_id"] for row in rows)
        if not consumer_ids:
            return ()
        placeholders = ",".join("?" for _ in consumer_ids)
        self.conn.execute(
            f"""UPDATE outbox_subscriptions SET enabled=0,updated_at=?
                WHERE enabled=1 AND consumer_id IN ({placeholders})""",
            (now, *consumer_ids),
        )
        for row in self.conn.execute("SELECT event_id FROM outbox"):
            self._refresh_event_locked(row["event_id"])
        return consumer_ids

    def _register_locked(
        self,
        consumer_id: str,
        event_types: Sequence[str],
        *,
        max_attempts: int,
        now: str,
    ) -> None:
        selected_types = tuple(event_types)
        placeholders = ",".join("?" for _ in selected_types)
        removed_types = [
            row["event_type"]
            for row in self.conn.execute(
                f"""SELECT event_type FROM outbox_subscriptions
                    WHERE consumer_id=? AND enabled=1
                      AND event_type NOT IN ({placeholders})""",
                (consumer_id, *selected_types),
            )
        ]
        if removed_types:
            self.conn.execute(
                f"""UPDATE outbox_subscriptions
                    SET enabled=0,updated_at=?
                    WHERE consumer_id=? AND enabled=1
                      AND event_type NOT IN ({placeholders})""",
                (now, consumer_id, *selected_types),
            )
            old_placeholders = ",".join("?" for _ in removed_types)
            for row in self.conn.execute(
                f"""SELECT event_id FROM outbox
                    WHERE event_type IN ({old_placeholders})""",
                removed_types,
            ):
                self._refresh_event_locked(row["event_id"])
        for event_type in event_types:
            existing = self.conn.execute(
                """SELECT max_attempts FROM outbox_subscriptions
                   WHERE consumer_id=? AND event_type=?""",
                (consumer_id, event_type),
            ).fetchone()
            if (
                existing is not None
                and int(existing["max_attempts"]) != max_attempts
            ):
                raise ValueError(
                    f"{consumer_id}/{event_type}: configured max_attempts "
                    f"{max_attempts} conflicts with persisted "
                    f"{existing['max_attempts']}"
                )
            self.conn.execute(
                """INSERT INTO outbox_subscriptions(
                       consumer_id,event_type,enabled,max_attempts,
                       created_at,updated_at
                   ) VALUES(?,?,1,?,?,?)
                   ON CONFLICT(consumer_id,event_type) DO UPDATE SET
                       enabled=1,updated_at=excluded.updated_at""",
                (consumer_id, event_type, max_attempts, now, now),
            )
            # Backfill events that predate this subscription or the
            # delivery trigger. New events are enqueued by the trigger.
            self.conn.execute(
                """INSERT OR IGNORE INTO outbox_deliveries(
                       event_id,consumer_id,status,attempt_count,available_at
                   )
                   SELECT o.event_id,?,'pending',0,
                          CASE WHEN o.available_at>? THEN o.available_at ELSE ? END
                   FROM outbox o
                   WHERE o.event_type=?""",
                (consumer_id, now, now, event_type),
            )
            # A newly introduced consumer temporarily reopens only the
            # historical events for which it now has unfinished work.
            self.conn.execute(
                """UPDATE outbox SET published_at=NULL
                   WHERE event_type=? AND event_id IN (
                       SELECT event_id FROM outbox_deliveries
                       WHERE consumer_id=?
                         AND status NOT IN ('succeeded','skipped','dead')
                   )""",
                (event_type, consumer_id),
            )

    def register(self, consumer: Consumer, *, max_attempts: int | None = None) -> None:
        """Atomically persist and activate a new in-process consumer."""
        consumer_id, event_types = _consumer_contract(consumer)
        superseded = _superseded_consumers(consumer, consumer_id)
        existing = self.consumers.get(consumer_id)
        if existing is not None and existing is not consumer:
            raise ValueError(f"duplicate consumer_id: {consumer_id}")
        active_conflicts = sorted(set(superseded) & set(self.consumers))
        if active_conflicts:
            raise ValueError(
                "active consumers cannot also be superseded: "
                + ", ".join(active_conflicts)
            )
        limit = int(max_attempts or self.default_max_attempts)
        if limit <= 0:
            raise ValueError("max_attempts must be positive")
        now = _iso(self.clock())
        self._begin()
        try:
            self._register_locked(
                consumer_id,
                event_types,
                max_attempts=limit,
                now=now,
            )
            disabled: list[str] = []
            for old_consumer_id in superseded:
                if self._disable_locked(old_consumer_id, None, now=now):
                    disabled.append(old_consumer_id)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        self.consumers[consumer_id] = consumer
        self.disabled_consumer_ids = tuple(
            sorted(set(self.disabled_consumer_ids) | set(disabled))
        )
        self._consumer_order = tuple(sorted(self.consumers))
        self._claim_cursor %= max(1, len(self._consumer_order))

    def _disable_locked(
        self,
        consumer_id: str,
        event_type: str | None,
        *,
        now: str,
    ) -> int:
        clauses = ["consumer_id=?", "enabled=1"]
        params: list[object] = [consumer_id]
        if event_type is not None:
            clauses.append("event_type=?")
            params.append(event_type)
        event_ids = tuple(
            row["event_id"]
            for row in self.conn.execute(
                f"""SELECT DISTINCT o.event_id
                    FROM outbox o JOIN outbox_subscriptions s
                      ON s.event_type=o.event_type
                    WHERE {' AND '.join('s.' + clause for clause in clauses)}""",
                params,
            )
        )
        cursor = self.conn.execute(
            f"""UPDATE outbox_subscriptions SET enabled=0,updated_at=?
                WHERE {' AND '.join(clauses)}""",
            (now, *params),
        )
        if cursor.rowcount:
            for event_id in event_ids:
                self._refresh_event_locked(event_id)
        return cursor.rowcount

    def disable(self, consumer_id: str, event_type: str | None = None) -> int:
        """Disable future gating for a consumer; existing receipts are retained."""
        self._begin()
        try:
            changed = self._disable_locked(
                consumer_id,
                event_type,
                now=_iso(self.clock()),
            )
            self.conn.commit()
            return changed
        except BaseException:
            self.conn.rollback()
            raise

    def disable_prefix(
        self,
        consumer_id_prefix: str,
        *,
        keep_consumer_ids: Sequence[str] = (),
    ) -> tuple[str, ...]:
        """Disable enabled generations sharing a stable consumer-ID prefix.

        Historical receipts and attempt history are retained. ``keep`` allows
        a managed deployment to activate one generation while atomically
        retiring every older generation for the same logical consumer.
        """

        managed = self._normalize_managed_generations(
            {consumer_id_prefix: tuple(keep_consumer_ids)}
        )
        prefix, keep = managed[0]
        self._begin()
        try:
            disabled = self._disable_prefix_locked(
                prefix,
                keep_consumer_ids=keep,
                now=_iso(self.clock()),
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
        self.disabled_consumer_ids = tuple(
            sorted(set(self.disabled_consumer_ids) | set(disabled))
        )
        return disabled

    def requeue_dead(
        self,
        consumer_id: str,
        *,
        event_type: str | None = None,
        limit: int = 100,
    ) -> tuple[str, ...]:
        """Explicitly replay dead active receipts while retaining attempt history."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        now = _iso(self.clock())
        clauses = [
            "d.consumer_id=?",
            "d.status='dead'",
            "s.enabled=1",
        ]
        params: list[object] = [consumer_id]
        if event_type is not None:
            clauses.append("o.event_type=?")
            params.append(event_type)
        params.append(int(limit))
        self._begin()
        try:
            rows = self.conn.execute(
                f"""SELECT d.event_id
                    FROM outbox_deliveries d
                    JOIN outbox o ON o.event_id=d.event_id
                    JOIN outbox_subscriptions s
                      ON s.consumer_id=d.consumer_id
                     AND s.event_type=o.event_type
                    WHERE {' AND '.join(clauses)}
                    ORDER BY d.completed_at,d.event_id
                    LIMIT ?""",
                params,
            ).fetchall()
            event_ids = tuple(row["event_id"] for row in rows)
            for event_id in event_ids:
                self.conn.execute(
                    """UPDATE outbox_deliveries
                       SET status='pending',attempt_count=0,
                           replay_count=replay_count+1,available_at=?,
                           locked_by=NULL,lock_token=NULL,locked_until=NULL,
                           started_at=NULL,completed_at=NULL,last_error=NULL,
                           result_json=NULL
                       WHERE event_id=? AND consumer_id=? AND status='dead'""",
                    (now, event_id, consumer_id),
                )
                self._refresh_event_locked(event_id)
            self.conn.commit()
            return event_ids
        except BaseException:
            self.conn.rollback()
            raise

    def _dead_letter_exhausted_locked(
        self,
        *,
        consumer_ids: Sequence[str],
        now: str,
    ) -> tuple[DeliveryFailure, ...]:
        """Fence crashed final attempts instead of reclaiming them forever."""
        placeholders = ",".join("?" for _ in consumer_ids)
        rows = self.conn.execute(
            f"""
            SELECT d.event_id,d.consumer_id,d.status,d.last_error,
                   d.attempt_count,d.replay_count
            FROM outbox_deliveries d
            JOIN outbox o ON o.event_id=d.event_id
            JOIN outbox_subscriptions s
              ON s.consumer_id=d.consumer_id
             AND s.event_type=o.event_type
             AND s.enabled=1
            WHERE d.consumer_id IN ({placeholders})
              AND d.attempt_count>=s.max_attempts
              AND (
                (d.status IN ('pending','retryable') AND d.available_at<=?)
                OR
                (d.status='running' AND d.locked_until<=?)
              )
            """,
            (*consumer_ids, now, now),
        ).fetchall()
        failures: list[DeliveryFailure] = []
        for row in rows:
            reason = row["last_error"] or (
                "delivery attempt budget exhausted after worker lease expired"
                if row["status"] == "running"
                else "delivery attempt budget exhausted"
            )
            cursor = self.conn.execute(
                """UPDATE outbox_deliveries
                   SET status='dead',available_at=?,locked_by=NULL,
                       lock_token=NULL,locked_until=NULL,completed_at=?,
                       last_error=?,result_json=?
                   WHERE event_id=? AND consumer_id=?
                     AND status=?""",
                (
                    now,
                    now,
                    reason,
                    _json({"status": "dead", "reason": reason, "detail": {}}),
                    row["event_id"],
                    row["consumer_id"],
                    row["status"],
                ),
            )
            if cursor.rowcount != 1:
                continue
            self.conn.execute(
                """UPDATE outbox_delivery_attempts
                   SET status='dead',completed_at=?,last_error=?,
                       result_json=?
                   WHERE event_id=? AND consumer_id=? AND attempt_number=?
                     AND replay_number=? AND status='running'""",
                (
                    now,
                    reason,
                    _json({"status": "dead", "reason": reason, "detail": {}}),
                    row["event_id"],
                    row["consumer_id"],
                    int(row["attempt_count"]),
                    int(row["replay_count"]),
                ),
            )
            self._refresh_event_locked(row["event_id"])
            failures.append(
                DeliveryFailure(
                    row["event_id"],
                    row["consumer_id"],
                    "dead",
                    reason,
                )
            )
        return tuple(failures)

    def claim(self) -> DeliveryClaim | None:
        if not self.consumers:
            return None
        now_dt = self.clock()
        now = _iso(now_dt)
        locked_until = _iso(now_dt + timedelta(seconds=self.lease_seconds))
        consumer_ids = self._consumer_order
        if not consumer_ids:
            return None
        priority_ids = (
            consumer_ids[self._claim_cursor :]
            + consumer_ids[: self._claim_cursor]
        )
        placeholders = ",".join("?" for _ in consumer_ids)
        priority_case = "CASE d.consumer_id " + " ".join(
            f"WHEN ? THEN {position}"
            for position, _consumer_id in enumerate(priority_ids)
        ) + f" ELSE {len(priority_ids)} END"
        self._begin()
        try:
            self._maintenance_failures.extend(
                self._dead_letter_exhausted_locked(
                    consumer_ids=consumer_ids,
                    now=now,
                )
            )
            row = self.conn.execute(
                f"""
                SELECT o.*,d.consumer_id,d.attempt_count,
                       d.replay_count,d.status AS delivery_status,
                       s.max_attempts
                FROM outbox_deliveries d
                JOIN outbox o ON o.event_id=d.event_id
                JOIN outbox_subscriptions s
                  ON s.consumer_id=d.consumer_id
                 AND s.event_type=o.event_type
                 AND s.enabled=1
                WHERE d.consumer_id IN ({placeholders})
                  AND d.attempt_count<s.max_attempts
                  AND (
                    (d.status IN ('pending','retryable') AND d.available_at<=?)
                    OR
                    (d.status='running' AND d.locked_until<=?)
                  )
                  AND NOT EXISTS (
                    SELECT 1
                    FROM outbox_deliveries active
                    WHERE active.consumer_id=d.consumer_id
                      AND active.status='running'
                      AND active.locked_until>?
                      AND active.event_id<>d.event_id
                  )
                ORDER BY {priority_case},
                         d.available_at,o.created_at,o.event_id,d.consumer_id
                LIMIT 1
                """,
                (*consumer_ids, now, now, now, *priority_ids),
            ).fetchone()
            if row is None:
                self.conn.commit()
                return None
            lock_token = uuid.uuid4().hex
            attempt = int(row["attempt_count"]) + 1
            cursor = self.conn.execute(
                """UPDATE outbox_deliveries
                   SET status='running',attempt_count=?,locked_by=?,
                       lock_token=?,locked_until=?,started_at=?,
                       completed_at=NULL
                   WHERE event_id=? AND consumer_id=?
                     AND (
                       (status IN ('pending','retryable') AND available_at<=?)
                       OR
                       (status='running' AND locked_until<=?)
                     )""",
                (
                    attempt,
                    self.worker_id,
                    lock_token,
                    locked_until,
                    now,
                    row["event_id"],
                    row["consumer_id"],
                    now,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                self.conn.rollback()
                return None
            if row["delivery_status"] == "running":
                self.conn.execute(
                    """UPDATE outbox_delivery_attempts
                       SET status='lease_expired',completed_at=?,
                           last_error=COALESCE(
                               last_error,'worker lease expired before completion'
                           )
                       WHERE event_id=? AND consumer_id=?
                         AND replay_number=? AND attempt_number=?
                         AND status='running'""",
                    (
                        now,
                        row["event_id"],
                        row["consumer_id"],
                        int(row["replay_count"]),
                        int(row["attempt_count"]),
                    ),
                )
            self.conn.execute(
                """INSERT INTO outbox_delivery_attempts(
                       event_id,consumer_id,replay_number,attempt_number,
                       lock_token,worker_id,status,started_at
                   ) VALUES(?,?,?,?,?,?,'running',?)""",
                (
                    row["event_id"],
                    row["consumer_id"],
                    int(row["replay_count"]),
                    attempt,
                    lock_token,
                    self.worker_id,
                    now,
                ),
            )
            payload = _decode_payload(row["payload_json"])
            event = OutboxEvent(
                event_id=row["event_id"],
                event_type=row["event_type"],
                aggregate_id=row["aggregate_id"],
                dedupe_key=row["dedupe_key"],
                payload=payload,
                created_at=row["created_at"],
                available_at=row["available_at"],
            )
            self.conn.commit()
            selected_position = consumer_ids.index(row["consumer_id"])
            self._claim_cursor = (selected_position + 1) % len(consumer_ids)
            return DeliveryClaim(
                event=event,
                consumer_id=row["consumer_id"],
                worker_id=self.worker_id,
                replay_number=int(row["replay_count"]),
                attempt=attempt,
                lock_token=lock_token,
                locked_until=locked_until,
                max_attempts=int(row["max_attempts"]),
            )
        except BaseException:
            self.conn.rollback()
            raise

    def _renew_with_connection(
        self,
        connection: sqlite3.Connection,
        claim: DeliveryClaim,
    ) -> bool:
        now_dt = self.clock()
        now = _iso(now_dt)
        locked_until = _iso(
            now_dt + timedelta(seconds=self.lease_seconds)
        )
        try:
            cursor = connection.execute(
                """UPDATE outbox_deliveries
                   SET locked_until=?
                   WHERE event_id=? AND consumer_id=? AND status='running'
                     AND locked_by=? AND lock_token=? AND locked_until>?""",
                (
                    locked_until,
                    claim.event.event_id,
                    claim.consumer_id,
                    claim.worker_id,
                    claim.lock_token,
                    now,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1
        except BaseException:
            connection.rollback()
            raise

    def _claim_additional(
        self,
        first: DeliveryClaim,
        *,
        limit: int,
    ) -> tuple[DeliveryClaim, ...]:
        """Extend a live consumer claim into one atomically fenced batch."""
        if limit <= 0:
            return ()
        now_dt = self.clock()
        now = _iso(now_dt)
        locked_until = _iso(
            now_dt + timedelta(seconds=self.lease_seconds)
        )
        self._begin()
        try:
            live = self.conn.execute(
                """SELECT 1 FROM outbox_deliveries
                   WHERE event_id=? AND consumer_id=? AND status='running'
                     AND locked_by=? AND lock_token=? AND locked_until>?""",
                (
                    first.event.event_id,
                    first.consumer_id,
                    first.worker_id,
                    first.lock_token,
                    now,
                ),
            ).fetchone()
            if live is None:
                raise DeliveryLeaseLost(
                    "first batch claim is no longer live: "
                    f"{first.event.event_id}/{first.consumer_id}"
                )
            rows = self.conn.execute(
                """SELECT o.*,d.attempt_count,d.replay_count,s.max_attempts
                   FROM outbox_deliveries d
                   JOIN outbox o ON o.event_id=d.event_id
                   JOIN outbox_subscriptions s
                     ON s.consumer_id=d.consumer_id
                    AND s.event_type=o.event_type
                    AND s.enabled=1
                   WHERE d.consumer_id=?
                     AND d.status IN ('pending','retryable')
                     AND d.available_at<=?
                     AND d.attempt_count<s.max_attempts
                   ORDER BY d.available_at,o.created_at,o.event_id
                   LIMIT ?""",
                (first.consumer_id, now, int(limit)),
            ).fetchall()
            claims: list[DeliveryClaim] = []
            for row in rows:
                lock_token = uuid.uuid4().hex
                attempt = int(row["attempt_count"]) + 1
                cursor = self.conn.execute(
                    """UPDATE outbox_deliveries
                       SET status='running',attempt_count=?,locked_by=?,
                           lock_token=?,locked_until=?,started_at=?,
                           completed_at=NULL
                       WHERE event_id=? AND consumer_id=?
                         AND status IN ('pending','retryable')
                         AND available_at<=?""",
                    (
                        attempt,
                        self.worker_id,
                        lock_token,
                        locked_until,
                        now,
                        row["event_id"],
                        first.consumer_id,
                        now,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        "could not fence an additional batch claim"
                    )
                self.conn.execute(
                    """INSERT INTO outbox_delivery_attempts(
                           event_id,consumer_id,replay_number,attempt_number,
                           lock_token,worker_id,status,started_at
                       ) VALUES(?,?,?,?,?,?,'running',?)""",
                    (
                        row["event_id"],
                        first.consumer_id,
                        int(row["replay_count"]),
                        attempt,
                        lock_token,
                        self.worker_id,
                        now,
                    ),
                )
                event = OutboxEvent(
                    event_id=row["event_id"],
                    event_type=row["event_type"],
                    aggregate_id=row["aggregate_id"],
                    dedupe_key=row["dedupe_key"],
                    payload=_decode_payload(row["payload_json"]),
                    created_at=row["created_at"],
                    available_at=row["available_at"],
                )
                claims.append(
                    DeliveryClaim(
                        event=event,
                        consumer_id=first.consumer_id,
                        worker_id=self.worker_id,
                        replay_number=int(row["replay_count"]),
                        attempt=attempt,
                        lock_token=lock_token,
                        locked_until=locked_until,
                        max_attempts=int(row["max_attempts"]),
                    )
                )
            self.conn.commit()
            return tuple(claims)
        except BaseException:
            self.conn.rollback()
            raise

    def renew(self, claim: DeliveryClaim) -> bool:
        """Extend a live claim using its worker/token fence."""
        return self._renew_with_connection(self.conn, claim)

    def complete(self, claim: DeliveryClaim, result: HandlerResult) -> str:
        if result.status not in {"succeeded", "skipped", "retryable", "dead"}:
            raise ValueError(f"invalid handler result status: {result.status}")
        if result.status in _SUCCESS_TERMINAL:
            status = result.status
            available_at = _iso(self.clock())
        elif result.status == "dead" or claim.attempt >= claim.max_attempts:
            status = "dead"
            available_at = _iso(self.clock())
        else:
            status = "retryable"
            available_at = _iso(
                self.clock()
                + timedelta(
                    seconds=self._retry_delay(
                        claim.attempt, result.retry_after_seconds
                    )
                )
            )
        error = result.reason if status in {"retryable", "dead"} else None
        now = _iso(self.clock())
        result_json = _json(
            {
                "status": status,
                "reason": result.reason,
                "detail": dict(result.detail or {}),
            }
        )
        self._begin()
        try:
            cursor = self.conn.execute(
                """UPDATE outbox_deliveries
                   SET status=?,available_at=?,locked_by=NULL,lock_token=NULL,
                       locked_until=NULL,completed_at=?,last_error=?,
                       result_json=?
                   WHERE event_id=? AND consumer_id=? AND status='running'
                     AND locked_by=? AND lock_token=?""",
                (
                    status,
                    available_at,
                    now,
                    error,
                    result_json,
                    claim.event.event_id,
                    claim.consumer_id,
                    claim.worker_id,
                    claim.lock_token,
                ),
            )
            if cursor.rowcount != 1:
                raise DeliveryLeaseLost(
                    f"delivery claim lost: {claim.event.event_id}/{claim.consumer_id}"
                )
            attempt_cursor = self.conn.execute(
                """UPDATE outbox_delivery_attempts
                   SET status=?,completed_at=?,last_error=?,result_json=?
                   WHERE event_id=? AND consumer_id=? AND attempt_number=?
                     AND replay_number=? AND lock_token=?
                     AND status='running'""",
                (
                    status,
                    now,
                    error,
                    result_json,
                    claim.event.event_id,
                    claim.consumer_id,
                    claim.attempt,
                    claim.replay_number,
                    claim.lock_token,
                ),
            )
            if attempt_cursor.rowcount != 1:
                raise RuntimeError(
                    "delivery attempt history is missing for a fenced claim"
                )
            self._refresh_event_locked(claim.event.event_id)
            self.conn.commit()
            return status
        except BaseException:
            self.conn.rollback()
            raise

    def fail(self, claim: DeliveryClaim, error: BaseException) -> str:
        return self.complete(
            claim,
            HandlerResult.retryable(
                f"{type(error).__name__}: {error}",
            ),
        )

    def _retry_delay(self, attempt: int, requested: int | None) -> int:
        if requested is not None:
            return max(1, min(int(requested), self.retry_max_seconds))
        exponent = max(0, min(int(attempt) - 1, 30))
        return min(
            self.retry_base_seconds * (2**exponent),
            self.retry_max_seconds,
        )

    def _refresh_event_locked(self, event_id: str) -> None:
        event = self.conn.execute(
            "SELECT event_type FROM outbox WHERE event_id=?", (event_id,)
        ).fetchone()
        if event is None:
            return
        rows = self.conn.execute(
            """SELECT d.status,d.attempt_count,d.last_error
               FROM outbox_subscriptions s
               LEFT JOIN outbox_deliveries d
                 ON d.consumer_id=s.consumer_id AND d.event_id=?
               WHERE s.event_type=? AND s.enabled=1""",
            (event_id, event["event_type"]),
        ).fetchall()
        attempts = int(
            self.conn.execute(
                """SELECT COUNT(*)
                   FROM outbox_delivery_attempts a
                   JOIN outbox_subscriptions s
                     ON s.consumer_id=a.consumer_id
                   WHERE a.event_id=? AND s.event_type=?
                     AND s.enabled=1""",
                (event_id, event["event_type"]),
            ).fetchone()[0]
        )
        errors = [row["last_error"] for row in rows if row["last_error"]]
        terminal = bool(rows) and all(
            row["status"] in _TERMINAL for row in rows
        )
        if terminal:
            self.conn.execute(
                """UPDATE outbox
                   SET published_at=COALESCE(published_at,?),
                       publish_attempts=?,last_error=?
                   WHERE event_id=?""",
                (
                    _iso(self.clock()),
                    attempts,
                    errors[-1] if errors else None,
                    event_id,
                ),
            )
        else:
            self.conn.execute(
                """UPDATE outbox
                   SET published_at=NULL,publish_attempts=?,last_error=?
                   WHERE event_id=?""",
                (
                    attempts,
                    errors[-1] if errors else None,
                    event_id,
                ),
            )

    def run(self, *, max_deliveries: int = 100) -> DispatchReport:
        if max_deliveries < 0:
            raise ValueError("max_deliveries cannot be negative")
        counts = {
            "claimed": 0,
            "succeeded": 0,
            "skipped": 0,
            "retryable": 0,
            "dead": 0,
        }
        failures: list[DeliveryFailure] = []

        def check_heartbeat(
            heartbeat: _LeaseHeartbeat,
            claims: Sequence[DeliveryClaim],
        ) -> None:
            if heartbeat.lost:
                raise DeliveryLeaseLost(
                    "a delivery lease expired while its handler was running"
                )
            if heartbeat.error is not None:
                for item in claims:
                    if not self.renew(item):
                        raise DeliveryLeaseLost(
                            "delivery heartbeat failed and a claim could not "
                            f"be renewed: {item.event.event_id}/{item.consumer_id}"
                        ) from heartbeat.error

        while counts["claimed"] < max_deliveries:
            first = self.claim()
            if self._maintenance_failures:
                maintenance = tuple(self._maintenance_failures)
                self._maintenance_failures.clear()
                counts["dead"] += len(maintenance)
                failures.extend(maintenance)
            if first is None:
                break
            consumer = self.consumers[first.consumer_id]
            batch_handler = getattr(consumer, "handle_batch", None)
            raw_batch_size = getattr(consumer, "batch_size", 1)
            batch_size = max(1, int(raw_batch_size))
            claims = [first]
            if callable(batch_handler) and batch_size > 1:
                remaining = max_deliveries - counts["claimed"] - 1
                claims.extend(
                    self._claim_additional(
                        first,
                        limit=min(batch_size - 1, remaining),
                    )
                )
            counts["claimed"] += len(claims)

            valid_claims: list[DeliveryClaim] = []
            for claim in claims:
                payload_error = claim.event.payload.get(
                    "_outbox_payload_error"
                )
                if not payload_error:
                    valid_claims.append(claim)
                    continue
                result = HandlerResult.dead(
                    f"corrupt outbox payload: {payload_error}"
                )
                status = self.complete(claim, result)
                counts[status] += 1
                failures.append(
                    DeliveryFailure(
                        claim.event.event_id,
                        claim.consumer_id,
                        status,
                        result.reason or status,
                    )
                )
            if not valid_claims:
                continue

            heartbeat = _LeaseHeartbeat(self, valid_claims)
            heartbeat.start()
            try:
                if callable(batch_handler):
                    raw_results = batch_handler(
                        [claim.event for claim in valid_claims],
                        [claim.context for claim in valid_claims],
                    )
                    if (
                        not isinstance(raw_results, Sequence)
                        or isinstance(raw_results, (str, bytes))
                        or len(raw_results) != len(valid_claims)
                    ):
                        raise TypeError(
                            "batch consumer must return one result per event"
                        )
                    results = [
                        _normalize_result(result)
                        for result in raw_results
                    ]
                else:
                    results = [
                        _normalize_result(
                            consumer.handle(
                                valid_claims[0].event,
                                valid_claims[0].context,
                            )
                        )
                    ]
            except Exception as exc:
                heartbeat.stop()
                check_heartbeat(heartbeat, valid_claims)
                for claim in valid_claims:
                    status = self.fail(claim, exc)
                    counts[status] += 1
                    failures.append(
                        DeliveryFailure(
                            claim.event.event_id,
                            claim.consumer_id,
                            status,
                            f"{type(exc).__name__}: {exc}",
                        )
                    )
            except BaseException as interrupt:
                heartbeat.stop()
                try:
                    check_heartbeat(heartbeat, valid_claims)
                    for claim in valid_claims:
                        try:
                            self.fail(claim, interrupt)
                        except DeliveryLeaseLost:
                            pass
                except Exception:
                    # Preserve process-control exceptions; an uncompleted
                    # fenced claim remains reclaimable after its lease.
                    pass
                raise
            else:
                heartbeat.stop()
                check_heartbeat(heartbeat, valid_claims)
                for claim, result in zip(
                    valid_claims, results, strict=True
                ):
                    status = self.complete(claim, result)
                    counts[status] += 1
                    if status in {"retryable", "dead"}:
                        failures.append(
                            DeliveryFailure(
                                claim.event.event_id,
                                claim.consumer_id,
                                status,
                                result.reason or status,
                            )
                        )
        return DispatchReport(
            claimed=counts["claimed"],
            succeeded=counts["succeeded"],
            skipped=counts["skipped"],
            retryable=counts["retryable"],
            dead=counts["dead"],
            failures=tuple(failures),
        )

    @classmethod
    def read_status(cls, database: str | Path) -> dict[str, Any]:
        """Read delivery health without applying migrations or opening writable."""
        path = Path(database)
        if not path.is_file():
            return {"initialized": False, "reason": "catalog_missing"}
        conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            tables = {
                row["name"]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required = {
                "outbox",
                "outbox_consumer_schema_migrations",
                "outbox_subscriptions",
                "outbox_deliveries",
                "outbox_delivery_attempts",
            }
            if not required <= tables:
                return {
                    "initialized": False,
                    "reason": "consumer_schema_missing",
                    "missing_tables": sorted(required - tables),
                }
            delivery_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(outbox_deliveries)"
                )
            }
            attempt_columns = {
                row["name"]
                for row in conn.execute(
                    "PRAGMA table_info(outbox_delivery_attempts)"
                )
            }
            missing_columns = {
                "outbox_deliveries": sorted(
                    {
                        "event_id",
                        "consumer_id",
                        "status",
                        "available_at",
                        "locked_until",
                        "replay_count",
                    }
                    - delivery_columns
                ),
                "outbox_delivery_attempts": sorted(
                    {
                        "event_id",
                        "consumer_id",
                        "replay_number",
                        "attempt_number",
                        "status",
                    }
                    - attempt_columns
                ),
            }
            missing_columns = {
                table: columns
                for table, columns in missing_columns.items()
                if columns
            }
            consumer_schema_version = int(
                conn.execute(
                    """SELECT COALESCE(MAX(version),0)
                       FROM outbox_consumer_schema_migrations"""
                ).fetchone()[0]
            )
            if missing_columns or consumer_schema_version < 2:
                return {
                    "initialized": False,
                    "reason": "consumer_schema_outdated",
                    "consumer_schema_version": consumer_schema_version,
                    "required_consumer_schema_version": 2,
                    "missing_columns": missing_columns,
                }
            now = _iso(_utc_now())
            by_status = {
                row["status"]: row["count"]
                for row in conn.execute(
                    """SELECT d.status,COUNT(*) AS count
                       FROM outbox_deliveries d
                       JOIN outbox o ON o.event_id=d.event_id
                       JOIN outbox_subscriptions s
                         ON s.consumer_id=d.consumer_id
                        AND s.event_type=o.event_type
                        AND s.enabled=1
                       GROUP BY d.status"""
                )
            }
            ready_receipts = conn.execute(
                """SELECT COUNT(*)
                   FROM outbox_deliveries d
                   JOIN outbox o ON o.event_id=d.event_id
                   JOIN outbox_subscriptions s
                     ON s.consumer_id=d.consumer_id
                    AND s.event_type=o.event_type
                    AND s.enabled=1
                   WHERE d.status IN ('pending','retryable')
                     AND d.available_at<=?""",
                (now,),
            ).fetchone()[0]
            expired_running = conn.execute(
                """SELECT COUNT(*)
                   FROM outbox_deliveries d
                   JOIN outbox o ON o.event_id=d.event_id
                   JOIN outbox_subscriptions s
                     ON s.consumer_id=d.consumer_id
                    AND s.event_type=o.event_type
                    AND s.enabled=1
                   WHERE d.status='running' AND d.locked_until<=?""",
                (now,),
            ).fetchone()[0]
            missing_receipts = conn.execute(
                """SELECT COUNT(*)
                   FROM outbox o
                   JOIN outbox_subscriptions s
                     ON s.event_type=o.event_type AND s.enabled=1
                   LEFT JOIN outbox_deliveries d
                     ON d.event_id=o.event_id
                    AND d.consumer_id=s.consumer_id
                   WHERE d.event_id IS NULL"""
            ).fetchone()[0]
            return {
                "initialized": True,
                "consumer_schema_version": consumer_schema_version,
                "events": conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0],
                "unpublished_events": conn.execute(
                    "SELECT COUNT(*) FROM outbox WHERE published_at IS NULL"
                ).fetchone()[0],
                "ready_unpublished_events": conn.execute(
                    """SELECT COUNT(*) FROM outbox
                       WHERE published_at IS NULL AND available_at<=?""",
                    (now,),
                ).fetchone()[0],
                "future_unpublished_events": conn.execute(
                    """SELECT COUNT(*) FROM outbox
                       WHERE published_at IS NULL AND available_at>?""",
                    (now,),
                ).fetchone()[0],
                "enabled_subscriptions": conn.execute(
                    """SELECT COUNT(*) FROM outbox_subscriptions
                       WHERE enabled=1"""
                ).fetchone()[0],
                "deliveries": by_status,
                "ready_receipts": ready_receipts,
                "expired_running_receipts": expired_running,
                "missing_receipts": missing_receipts,
            }
        finally:
            conn.close()


__all__ = [
    "DeliveryClaim",
    "DeliveryFailure",
    "DeliveryLeaseLost",
    "DispatchReport",
    "OutboxDispatcher",
]
