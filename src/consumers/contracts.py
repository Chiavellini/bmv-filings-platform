"""Stable contracts for independently delivered estate outbox events."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable


DeliveryStatus = Literal[
    "pending",
    "running",
    "succeeded",
    "skipped",
    "retryable",
    "dead",
]


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    event_id: str
    event_type: str
    aggregate_id: str
    dedupe_key: str
    payload: Mapping[str, Any]
    created_at: str
    available_at: str


@dataclass(frozen=True, slots=True)
class DeliveryContext:
    consumer_id: str
    worker_id: str
    attempt: int
    lock_token: str


@dataclass(frozen=True, slots=True)
class HandlerResult:
    status: Literal["succeeded", "skipped", "retryable", "dead"]
    reason: str | None = None
    detail: Mapping[str, Any] | None = None
    retry_after_seconds: int | None = None

    @classmethod
    def succeeded(
        cls, detail: Mapping[str, Any] | None = None
    ) -> "HandlerResult":
        return cls("succeeded", detail=detail)

    @classmethod
    def skipped(
        cls,
        reason: str,
        detail: Mapping[str, Any] | None = None,
    ) -> "HandlerResult":
        return cls("skipped", reason=reason, detail=detail)

    @classmethod
    def retryable(
        cls,
        reason: str,
        *,
        retry_after_seconds: int | None = None,
        detail: Mapping[str, Any] | None = None,
    ) -> "HandlerResult":
        return cls(
            "retryable",
            reason=reason,
            detail=detail,
            retry_after_seconds=retry_after_seconds,
        )

    @classmethod
    def dead(
        cls,
        reason: str,
        detail: Mapping[str, Any] | None = None,
    ) -> "HandlerResult":
        return cls("dead", reason=reason, detail=detail)


@runtime_checkable
class Consumer(Protocol):
    consumer_id: str
    event_types: Sequence[str]

    def handle(
        self,
        event: OutboxEvent,
        context: DeliveryContext,
    ) -> object: ...


__all__ = [
    "Consumer",
    "DeliveryContext",
    "DeliveryStatus",
    "HandlerResult",
    "OutboxEvent",
]
