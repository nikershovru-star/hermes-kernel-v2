"""kernel/events.py — Event Platform foundation (ADR-017).

Implements the v5 *Event Platform* foundation on top of the existing async
``EventBus`` (kernel.bus):

* LAYER 1 — ``DomainEvent``: a richer event (aggregate_id, timestamp, version)
  that **extends** the existing ``kernel.domain.Event`` so it flows through the
  existing ``EventBus.publish`` unchanged (no transport duplication, axis clean).
* LAYER 3 — ``EventStore``: append-only journal (in-memory; optional SQLite).
* LAYER 4 — CQRS: ``Command``/``CommandBus`` (commands trigger domain logic that
  *emits* events) and ``ReadModel`` projections.
* LAYER 5 — ``Query``/``QueryBus``: ask projections.

AXIS CONTRACT: depends ONLY on kernel.domain (+ re-exports kernel.bus.EventBus).
Never imports plugins. Events are pure data; handlers are injected at runtime.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import uuid
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from pydantic import Field

from kernel.bus import EventBus
from kernel.domain import BaseEntity, Event

logger = logging.getLogger("hermes.kernel.events")


# --------------------------------------------------------------------------- #
# LAYER 1 — Domain Events
# --------------------------------------------------------------------------- #
class DomainEvent(Event):
    """Richer domain event for the v5 Event Platform (ADR-017).

    Extends ``kernel.domain.Event`` so the existing async ``EventBus`` accepts
    it directly (isinstance(Event) holds). Adds the CQRS essentials:

    * ``aggregate_id`` — the entity the event belongs to (agent_id, task_id, ...).
    * ``timestamp`` — wall-clock UTC datetime frozen at creation (separate from
      the inherited ``created_at`` string, for query/replay convenience).
    * ``version`` — event schema version (supports future evolution).
    """

    aggregate_id: str = ""
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    version: int = 1


# --------------------------------------------------------------------------- #
# LAYER 3 — Event Store (append-only journal)
# --------------------------------------------------------------------------- #
class EventStore:
    """Append-only persistence of ``DomainEvent`` (ADR-017, Decision E).

    Primary: in-memory list. Optional: a SQLite table created on demand. Events
    are **never mutated** — ``append`` only; any attempt to rewrite is rejected
    by the append-only invariant (we simply never expose an update path).
    """

    def __init__(self, sqlite_path: str | None = None) -> None:
        self._events: list[DomainEvent] = []
        self._db: sqlite3.Connection | None = None
        if sqlite_path is not None:
            self._db = sqlite3.connect(sqlite_path, check_same_thread=False)
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS events ("
                "id TEXT PRIMARY KEY, aggregate_id TEXT, event_type TEXT, "
                "payload_json TEXT, timestamp TEXT, version INTEGER)"
            )
            self._db.commit()

    async def append(self, event: DomainEvent) -> None:
        """Append ``event`` to the journal (in-memory + SQLite if configured)."""
        if not isinstance(event, DomainEvent):
            raise TypeError(f"EventStore.append expects DomainEvent, got {type(event).__name__}")
        self._events.append(event)
        if self._db is not None:
            self._db.execute(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)",
                (
                    event.id,
                    event.aggregate_id,
                    event.type,
                    __import__("json").dumps(event.payload),
                    event.timestamp.isoformat(),
                    event.version,
                ),
            )
            self._db.commit()

    async def read_stream(self, aggregate_id: str) -> list[DomainEvent]:
        """All events for one aggregate, in append order."""
        return [e for e in self._events if e.aggregate_id == aggregate_id]

    async def read_all(self, since: datetime | None = None) -> list[DomainEvent]:
        """All events, optionally filtered to those at/after ``since``."""
        if since is None:
            return list(self._events)
        return [e for e in self._events if e.timestamp >= since]

    def count(self) -> int:
        return len(self._events)

    def close(self) -> None:
        if self._db is not None:
            self._db.close()
            self._db = None


# --------------------------------------------------------------------------- #
# LAYER 4 — CQRS: Command Bus + Read Models
# --------------------------------------------------------------------------- #
class Command(ABC):
    """A Command is intent to change state. It is NOT an event.

    Handling a command produces domain events (via the injected EventBus /
    EventStore). ``aggregate_id`` routes the command to the right handler.
    """

    aggregate_id: str

    def __init__(self, aggregate_id: str) -> None:
        self.aggregate_id = aggregate_id


CommandHandler = Callable[[Command], Awaitable[None]]


class CommandBus:
    """Dispatches ``Command`` objects to registered async handlers.

    Handlers are responsible for performing domain logic and emitting
    ``DomainEvent``s through the injected ``EventBus`` (and/or ``EventStore``).
    """

    def __init__(self, bus: EventBus, store: EventStore | None = None) -> None:
        self._bus = bus
        self._store = store
        self._handlers: dict[type[Command], CommandHandler] = {}

    def register(self, command_type: type[Command], handler: CommandHandler) -> None:
        self._handlers[command_type] = handler

    async def send(self, command: Command) -> None:
        handler = self._handlers.get(type(command))
        if handler is None:
            raise KeyError(f"no handler for command {type(command).__name__}")
        await handler(command)

    async def publish_event(self, event: DomainEvent) -> None:
        """Convenience: append to store + publish on bus in one call."""
        if self._store is not None:
            await self._store.append(event)
        self._bus.publish(event)


class ReadModel(ABC):
    """A projection that folds ``DomainEvent``s into queryable state."""

    @abstractmethod
    async def handle(self, event: DomainEvent) -> None:
        ...

    @abstractmethod
    def reset(self) -> None:
        ...


# --------------------------------------------------------------------------- #
# LAYER 5 — Query Bus
# --------------------------------------------------------------------------- #
class Query(ABC):
    """A read-only question answered by a ``ReadModel`` projection."""


QueryHandler = Callable[[Query], Awaitable[Any]]


class QueryBus:
    """Routes ``Query`` objects to registered async handlers (projections)."""

    def __init__(self) -> None:
        self._handlers: dict[type[Query], QueryHandler] = {}

    def register(self, query_type: type[Query], handler: QueryHandler) -> None:
        self._handlers[query_type] = handler

    async def ask(self, query: Query) -> Any:
        handler = self._handlers.get(type(query))
        if handler is None:
            raise KeyError(f"no handler for query {type(query).__name__}")
        return await handler(query)


# --------------------------------------------------------------------------- #
# Workflow / Execution Platform events (ADR-019)
# --------------------------------------------------------------------------- #
class WorkflowStepStarted(DomainEvent):
    """A workflow step began executing."""

    def __init__(self, instance_id: str, step_id: str, capability: str) -> None:
        super().__init__(
            type="workflow.step_started",
            aggregate_id=instance_id,
            payload={"step_id": step_id, "capability": capability},
        )


class WorkflowStepCompleted(DomainEvent):
    """A workflow step finished successfully."""

    def __init__(self, instance_id: str, step_id: str, artifact_id: str, duration_ms: float) -> None:
        super().__init__(
            type="workflow.step_completed",
            aggregate_id=instance_id,
            payload={"step_id": step_id, "artifact_id": artifact_id, "duration_ms": duration_ms},
        )


class WorkflowStepFailed(DomainEvent):
    """A workflow step failed (may retry or compensate)."""

    def __init__(self, instance_id: str, step_id: str, error: str, attempt: int, will_retry: bool) -> None:
        super().__init__(
            type="workflow.step_failed",
            aggregate_id=instance_id,
            payload={"step_id": step_id, "error": error, "attempt": attempt, "will_retry": will_retry},
        )


class WorkflowStepAwaitingApproval(DomainEvent):
    """A step with ``requires_approval`` paused the workflow."""

    def __init__(self, instance_id: str, step_id: str, reason: str) -> None:
        super().__init__(
            type="workflow.step_awaiting_approval",
            aggregate_id=instance_id,
            payload={"step_id": step_id, "reason": reason},
        )


class WorkflowCompensating(DomainEvent):
    """A compensation step is running for a failed step."""

    def __init__(self, instance_id: str, failed_step: str, compensation_step: str) -> None:
        super().__init__(
            type="workflow.compensating",
            aggregate_id=instance_id,
            payload={"failed_step": failed_step, "compensation_step": compensation_step},
        )


__all__ = [
    "DomainEvent",
    "EventStore",
    "Command",
    "CommandBus",
    "ReadModel",
    "Query",
    "QueryBus",
    "EventBus",
    "WorkflowStepStarted",
    "WorkflowStepCompleted",
    "WorkflowStepFailed",
    "WorkflowStepAwaitingApproval",
    "WorkflowCompensating",
]
