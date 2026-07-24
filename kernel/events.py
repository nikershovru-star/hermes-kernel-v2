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


# --------------------------------------------------------------------------- #
# Sandbox events (ADR-020)
# --------------------------------------------------------------------------- #
class SandboxViolationEvent(DomainEvent):
    """A sandbox policy was breached (timeout / memory / cpu / file / ...)."""

    def __init__(
        self,
        aggregate_id: str,
        violation_type: str,
        policy: dict[str, Any],
        details: dict[str, Any],
    ) -> None:
        super().__init__(
            type="sandbox.violation",
            aggregate_id=aggregate_id,
            payload={
                "violation_type": violation_type,
                "policy": policy,
                "details": details,
            },
        )


class SandboxCleanupCompleted(DomainEvent):
    """Cleanup after a sandbox breach finished (success or failure)."""

    def __init__(self, aggregate_id: str, success: bool, error: str | None) -> None:
        super().__init__(
            type="sandbox.cleanup_completed",
            aggregate_id=aggregate_id,
            payload={"success": success, "error": error},
        )


# --------------------------------------------------------------------------- #
# Health & Recovery events (ADR-021)
# --------------------------------------------------------------------------- #
class AgentUnhealthy(DomainEvent):
    """A component's health probe crossed its failure threshold."""

    def __init__(self, component_id: str, last_error: str | None, consecutive_failures: int) -> None:
        super().__init__(
            type="health.agent_unhealthy",
            aggregate_id=component_id,
            payload={"last_error": last_error, "consecutive_failures": consecutive_failures},
        )


class AgentRecovered(DomainEvent):
    """A previously-unhealthy component was restarted / recovered."""

    def __init__(self, component_id: str, restart_count: int) -> None:
        super().__init__(
            type="health.agent_recovered",
            aggregate_id=component_id,
            payload={"restart_count": restart_count},
        )


class WorkflowStalled(DomainEvent):
    """A workflow instance stalled on a failed step (no forward progress)."""

    def __init__(self, instance_id: str, failed_step_id: str, error: str) -> None:
        super().__init__(
            type="health.workflow_stalled",
            aggregate_id=instance_id,
            payload={"failed_step_id": failed_step_id, "error": error},
        )


class DeadLetterAppended(DomainEvent):
    """A failed task/event/step was appended to the dead-letter queue."""

    def __init__(self, entry_id: str, component_id: str, entry_type: str, retry_count: int) -> None:
        super().__init__(
            type="health.dead_letter_appended",
            aggregate_id=component_id,
            payload={"entry_id": entry_id, "entry_type": entry_type, "retry_count": retry_count},
        )


class DeadLetterRecovered(DomainEvent):
    """A dead-letter entry was successfully recovered / replayed."""

    def __init__(self, entry_id: str, component_id: str) -> None:
        super().__init__(
            type="health.dead_letter_recovered",
            aggregate_id=component_id,
            payload={"entry_id": entry_id},
        )


class CircuitBreakerTripped(DomainEvent):
    """A circuit breaker changed state (closed / open / half_open)."""

    def __init__(self, capability: str, state: str, failure_count: int) -> None:
        super().__init__(
            type="health.circuit_breaker_tripped",
            aggregate_id=capability,
            payload={"state": state, "failure_count": failure_count},
        )


# --------------------------------------------------------------------------- #
# Behavior Engine events (ADR-022)
# --------------------------------------------------------------------------- #
class MouseMoved(DomainEvent):
    """Mouse moved to a new position along a curve."""

    def __init__(
        self,
        agent_id: str,
        from_pos: tuple[int, int],
        to_pos: tuple[int, int],
        duration_ms: float,
        curve_type: str,
    ) -> None:
        super().__init__(
            type="behavior.mouse_moved",
            aggregate_id=agent_id,
            payload={
                "from_pos": list(from_pos),
                "to_pos": list(to_pos),
                "duration_ms": duration_ms,
                "curve_type": curve_type,
            },
        )


class MouseClicked(DomainEvent):
    """Mouse clicked after a gaze fixation."""

    def __init__(self, agent_id: str, position: tuple[int, int], fixation_ms: int, button: str) -> None:
        super().__init__(
            type="behavior.mouse_clicked",
            aggregate_id=agent_id,
            payload={"position": list(position), "fixation_ms": fixation_ms, "button": button},
        )


class Scrolled(DomainEvent):
    """Scroll with momentum completed."""

    def __init__(self, agent_id: str, direction: str, distance_px: int, pauses_ms: list[int]) -> None:
        super().__init__(
            type="behavior.scrolled",
            aggregate_id=agent_id,
            payload={"direction": direction, "distance_px": distance_px, "pauses_ms": pauses_ms},
        )


class TextTyped(DomainEvent):
    """Text typed with human rhythm."""

    def __init__(self, agent_id: str, text: str, wpm: int, error_count: int, duration_ms: float) -> None:
        super().__init__(
            type="behavior.text_typed",
            aggregate_id=agent_id,
            payload={"text": text, "wpm": wpm, "error_count": error_count, "duration_ms": duration_ms},
        )


class GazeFixated(DomainEvent):
    """Gaze fixed on a point (reading / pre-click)."""

    def __init__(self, agent_id: str, position: tuple[int, int], duration_ms: int) -> None:
        super().__init__(
            type="behavior.gaze_fixated",
            aggregate_id=agent_id,
            payload={"position": list(position), "duration_ms": duration_ms},
        )


class ReadingProgress(DomainEvent):
    """Reading simulation progressed."""

    def __init__(self, agent_id: str, words_read: int, regressions: int, duration_ms: float) -> None:
        super().__init__(
            type="behavior.reading_progress",
            aggregate_id=agent_id,
            payload={"words_read": words_read, "regressions": regressions, "duration_ms": duration_ms},
        )


# --------------------------------------------------------------------------- #
# Swarm / Teams events (ADR-023)
# --------------------------------------------------------------------------- #
class AgentJoinedSwarm(DomainEvent):
    """An agent joined a swarm."""

    def __init__(self, swarm_id: str, agent_id: str, node_id: str, role: str, capabilities: list[str]) -> None:
        super().__init__(
            type="swarm.agent_joined",
            aggregate_id=swarm_id,
            payload={"agent_id": agent_id, "node_id": node_id, "role": role, "capabilities": capabilities},
        )


class AgentLeftSwarm(DomainEvent):
    """An agent left a swarm (graceful / timeout / failure)."""

    def __init__(self, swarm_id: str, agent_id: str, reason: str) -> None:
        super().__init__(
            type="swarm.agent_left",
            aggregate_id=swarm_id,
            payload={"agent_id": agent_id, "reason": reason},
        )


class HeartbeatReceived(DomainEvent):
    """A heartbeat arrived from a node/agent."""

    def __init__(self, node_id: str, agent_id: str, timestamp: float, load_score: float) -> None:
        super().__init__(
            type="swarm.heartbeat_received",
            aggregate_id=node_id,
            payload={"agent_id": agent_id, "timestamp": timestamp, "load_score": load_score},
        )


class HeartbeatMissed(DomainEvent):
    """A node/agent missed one or more expected heartbeats."""

    def __init__(self, node_id: str, agent_id: str, missed_count: int, suspicion_level: str) -> None:
        super().__init__(
            type="swarm.heartbeat_missed",
            aggregate_id=node_id,
            payload={"agent_id": agent_id, "missed_count": missed_count, "suspicion_level": suspicion_level},
        )


class LeaderElected(DomainEvent):
    """A new leader was elected for a swarm (bully algorithm)."""

    def __init__(self, swarm_id: str, leader_id: str, previous_leader_id: str | None, algorithm: str = "bully") -> None:
        super().__init__(
            type="swarm.leader_elected",
            aggregate_id=swarm_id,
            payload={"leader_id": leader_id, "previous_leader_id": previous_leader_id, "algorithm": algorithm},
        )


class TaskDelegated(DomainEvent):
    """A task was delegated from one agent to another within a swarm."""

    def __init__(self, delegation_id: str, task_id: str, from_agent: str, to_agent: str, swarm_id: str) -> None:
        super().__init__(
            type="swarm.task_delegated",
            aggregate_id=swarm_id,
            payload={
                "delegation_id": delegation_id,
                "task_id": task_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
            },
        )


class TaskCompleted(DomainEvent):
    """A delegated task finished."""

    def __init__(self, delegation_id: str, task_id: str, result_summary: str) -> None:
        super().__init__(
            type="swarm.task_completed",
            aggregate_id=delegation_id,
            payload={"task_id": task_id, "result_summary": result_summary},
        )


class NodePartitioned(DomainEvent):
    """A node was partitioned (declared unreachable/unhealthy)."""

    def __init__(self, node_id: str, partition_reason: str, affected_agents: list[str]) -> None:
        super().__init__(
            type="swarm.node_partitioned",
            aggregate_id=node_id,
            payload={"partition_reason": partition_reason, "affected_agents": affected_agents},
        )


# --------------------------------------------------------------------------- #
# Dynamic Planner events (ADR-024)
# --------------------------------------------------------------------------- #
class PlanCreated(DomainEvent):
    """A dynamic plan was created for a workflow."""

    def __init__(self, plan_id: str, workflow_id: str, step_count: int, version: int) -> None:
        super().__init__(
            type="planner.plan_created",
            aggregate_id=plan_id,
            payload={"plan_id": plan_id, "workflow_id": workflow_id, "step_count": step_count, "version": version},
        )


class StepPlanned(DomainEvent):
    """A step was added to a plan."""

    def __init__(self, plan_id: str, step_id: str, capability: str, agent_id: str | None, risk: str) -> None:
        super().__init__(
            type="planner.step_planned",
            aggregate_id=plan_id,
            payload={"plan_id": plan_id, "step_id": step_id, "capability": capability, "agent_id": agent_id, "risk": risk},
        )


class ReplanTriggered(DomainEvent):
    """A replan was triggered by a failure / risk / swarm signal."""

    def __init__(self, trigger_id: str, plan_id: str, reason: str, failed_step_id: str | None) -> None:
        super().__init__(
            type="planner.replan_triggered",
            aggregate_id=plan_id,
            payload={"trigger_id": trigger_id, "plan_id": plan_id, "reason": reason, "failed_step_id": failed_step_id},
        )


class PlanAdapted(DomainEvent):
    """A plan was adapted (replanned) into a new version."""

    def __init__(self, plan_id: str, old_version: int, new_version: int, changes_summary: str) -> None:
        super().__init__(
            type="planner.plan_adapted",
            aggregate_id=plan_id,
            payload={"plan_id": plan_id, "old_version": old_version, "new_version": new_version, "changes_summary": changes_summary},
        )


class StepExecuted(DomainEvent):
    """A plan step finished execution."""

    def __init__(self, plan_id: str, step_id: str, status: str, duration_ms: int, retry_count: int) -> None:
        super().__init__(
            type="planner.step_executed",
            aggregate_id=plan_id,
            payload={"plan_id": plan_id, "step_id": step_id, "status": status, "duration_ms": duration_ms, "retry_count": retry_count},
        )


class RiskEscalated(DomainEvent):
    """A step's risk level was escalated by risk assessment."""

    def __init__(self, plan_id: str, step_id: str, from_risk: str, to_risk: str, reason: str) -> None:
        super().__init__(
            type="planner.risk_escalated",
            aggregate_id=plan_id,
            payload={"plan_id": plan_id, "step_id": step_id, "from_risk": from_risk, "to_risk": to_risk, "reason": reason},
        )


# --------------------------------------------------------------------------- #
# ADR-025 — Knowledge Graph & Semantic Memory
# --------------------------------------------------------------------------- #
class EntityDiscovered(DomainEvent):
    """A new entity was discovered/added to a knowledge graph."""

    def __init__(self, graph_id: str, entity_id: str, name: str, type: str, source: str, confidence: float) -> None:
        super().__init__(
            type="kg.entity_discovered",
            aggregate_id=graph_id,
            payload={"graph_id": graph_id, "entity_id": entity_id, "name": name, "type": type, "source": source, "confidence": confidence},
        )


class RelationCreated(DomainEvent):
    """A relation was created between two entities."""

    def __init__(self, graph_id: str, relation_id: str, source_id: str, target_id: str, type: str, weight: float) -> None:
        super().__init__(
            type="kg.relation_created",
            aggregate_id=graph_id,
            payload={"graph_id": graph_id, "relation_id": relation_id, "source_id": source_id, "target_id": target_id, "type": type, "weight": weight},
        )


class GraphUpdated(DomainEvent):
    """A knowledge graph was mutated (entity/relation added/merged/deleted)."""

    def __init__(self, graph_id: str, version: int, change_summary: str) -> None:
        super().__init__(
            type="kg.graph_updated",
            aggregate_id=graph_id,
            payload={"graph_id": graph_id, "version": version, "change_summary": change_summary},
        )


class QueryExecuted(DomainEvent):
    """A graph query was executed."""

    def __init__(self, query_id: str, graph_id: str, query_type: str, result_count: int, duration_ms: int) -> None:
        super().__init__(
            type="kg.query_executed",
            aggregate_id=graph_id,
            payload={"query_id": query_id, "graph_id": graph_id, "query_type": query_type, "result_count": result_count, "duration_ms": duration_ms},
        )


class InferenceFired(DomainEvent):
    """An inference rule matched and fired over the graph."""

    def __init__(self, graph_id: str, rule_id: str, matched_entities: list[str], action_taken: str) -> None:
        super().__init__(
            type="kg.inference_fired",
            aggregate_id=graph_id,
            payload={"graph_id": graph_id, "rule_id": rule_id, "matched_entities": matched_entities, "action_taken": action_taken},
        )


class EntityMerged(DomainEvent):
    """Two or more entities were merged into a canonical entity."""

    def __init__(self, graph_id: str, canonical_id: str, merged_ids: list[str], reason: str) -> None:
        super().__init__(
            type="kg.entity_merged",
            aggregate_id=graph_id,
            payload={"graph_id": graph_id, "canonical_id": canonical_id, "merged_ids": merged_ids, "reason": reason},
        )


# -- plugin marketplace / multi-node (ADR-026) ------------------------- #
class PluginDiscovered(DomainEvent):
    """A new plugin was discovered in a (remote) catalog."""

    def __init__(self, package_id: str, name: str, source: str, version: str, source_url: str) -> None:
        super().__init__(
            type="mp.plugin_discovered",
            aggregate_id=package_id,
            payload={"package_id": package_id, "name": name, "source": source, "version": version, "source_url": source_url},
        )


class PluginInstalled(DomainEvent):
    """A plugin package was successfully installed."""

    def __init__(self, package_id: str, name: str, version: str, source: str) -> None:
        super().__init__(
            type="mp.plugin_installed",
            aggregate_id=package_id,
            payload={"package_id": package_id, "name": name, "version": version, "source": source},
        )


class PluginInstallFailed(DomainEvent):
    """A plugin installation failed."""

    def __init__(self, package_id: str, name: str, reason: str) -> None:
        super().__init__(
            type="mp.plugin_install_failed",
            aggregate_id=package_id,
            payload={"package_id": package_id, "name": name, "reason": reason},
        )


class NodeJoined(DomainEvent):
    """A node joined the cluster."""

    def __init__(self, node_id: str, address: str, cluster_id: str, capabilities: list[str]) -> None:
        super().__init__(
            type="mp.node_joined",
            aggregate_id=node_id,
            payload={"node_id": node_id, "address": address, "cluster_id": cluster_id, "capabilities": capabilities},
        )


class NodeLeft(DomainEvent):
    """A node left (or was removed from) the cluster."""

    def __init__(self, node_id: str, cluster_id: str, reason: str = "") -> None:
        super().__init__(
            type="mp.node_left",
            aggregate_id=node_id,
            payload={"node_id": node_id, "cluster_id": cluster_id, "reason": reason},
        )


# -- observability (ADR-027) ------------------------------------------- #
class MetricRecorded(DomainEvent):
    """A metric sample was recorded (counter / histogram / gauge)."""

    def __init__(self, name: str, value: float, metric_type: str, labels: dict, aggregate_id: str = "") -> None:
        super().__init__(
            type="obs.metric_recorded",
            aggregate_id=aggregate_id,
            payload={"name": name, "value": value, "type": metric_type, "labels": labels},
        )


class TraceSpanStarted(DomainEvent):
    """A trace span began."""

    def __init__(self, span_id: str, trace_id: str, span_name: str, parent_id: str | None, correlation_id: str | None = None) -> None:
        super().__init__(
            type="obs.span_started",
            aggregate_id=trace_id,
            payload={"span_id": span_id, "trace_id": trace_id, "span_name": span_name, "parent_id": parent_id, "correlation_id": correlation_id},
        )


class TraceSpanFinished(DomainEvent):
    """A trace span completed."""

    def __init__(self, span_id: str, trace_id: str, status: str, correlation_id: str | None = None) -> None:
        super().__init__(
            type="obs.span_finished",
            aggregate_id=trace_id,
            payload={"span_id": span_id, "trace_id": trace_id, "status": status, "correlation_id": correlation_id},
        )


class LogEntryEmitted(DomainEvent):
    """A structured log line was emitted."""

    def __init__(self, level: str, message: str, correlation_id: str | None = None, context: dict | None = None) -> None:
        super().__init__(
            type="obs.log_emitted",
            aggregate_id=correlation_id or "",
            payload={"level": level, "message": message, "correlation_id": correlation_id, "context": context or {}},
        )


# -- security (ADR-028) ----------------------------------------------- #
class PermissionDenied(DomainEvent):
    """A guarded action was refused by ``CapabilityGuard`` (no matching permission)."""

    def __init__(self, principal: str, action: str, resource: str) -> None:
        super().__init__(
            type="sec.permission_denied",
            aggregate_id=principal,
            payload={"principal": principal, "action": action, "resource": resource},
        )


class ResourceLimitExceeded(DomainEvent):
    """A cooperative resource limit (calls / cpu_ms) was breached by a package."""

    def __init__(self, principal: str, limit_type: str, value: float) -> None:
        super().__init__(
            type="sec.resource_limit_exceeded",
            aggregate_id=principal,
            payload={"principal": principal, "limit_type": limit_type, "value": value},
        )


class PluginSandboxed(DomainEvent):
    """A package's sandbox policy was registered with the guard."""

    def __init__(self, package_id: str, policy_summary: str) -> None:
        super().__init__(
            type="sec.plugin_sandboxed",
            aggregate_id=package_id,
            payload={"package_id": package_id, "policy_summary": policy_summary},
        )


class AuditLogEntry(DomainEvent):
    """An audit entry was written by ``CapabilityGuard``."""

    def __init__(self, entry_id: str, who: str, action: str, resource: str, result: str, timestamp: str) -> None:
        super().__init__(
            type="sec.audit_entry",
            aggregate_id=who,
            payload={
                "entry_id": entry_id,
                "who": who,
                "action": action,
                "resource": resource,
                "result": result,
                "timestamp": timestamp,
            },
        )


# --------------------------------------------------------------------------- #
# MCP Gateway events (ADR-029) — namespaced ``mcp.*``
# --------------------------------------------------------------------------- #
class McpConnected(DomainEvent):
    """A client session to a remote MCP server was established."""

    def __init__(self, session_id: str, server_url: str, server_name: str = "", server_version: str = "") -> None:
        super().__init__(
            type="mcp.connected",
            aggregate_id=session_id,
            payload={
                "session_id": session_id,
                "server_url": server_url,
                "server_name": server_name,
                "server_version": server_version,
            },
        )


class McpToolCalled(DomainEvent):
    """A remote MCP tool was invoked through the gateway."""

    def __init__(self, session_id: str, tool_name: str, arguments_hash: str, latency_ms: float) -> None:
        super().__init__(
            type="mcp.tool_called",
            aggregate_id=session_id,
            payload={
                "tool_name": tool_name,
                "arguments_hash": arguments_hash,
                "latency_ms": latency_ms,
            },
        )


class McpResourceRead(DomainEvent):
    """A remote MCP resource was read through the gateway."""

    def __init__(self, session_id: str, uri: str, size_bytes: int) -> None:
        super().__init__(
            type="mcp.resource_read",
            aggregate_id=session_id,
            payload={"uri": uri, "size_bytes": size_bytes},
        )


class McpSessionClosed(DomainEvent):
    """A client session with a remote MCP server was closed."""

    def __init__(self, session_id: str, reason: str = "explicit_close") -> None:
        super().__init__(
            type="mcp.session_closed",
            aggregate_id=session_id,
            payload={"reason": reason},
        )


class McpError(DomainEvent):
    """A gateway operation against a remote MCP server failed."""

    def __init__(self, server_url: str, error_type: str, message: str) -> None:
        super().__init__(
            type="mcp.error",
            aggregate_id=server_url,
            payload={"error_type": error_type, "message": message},
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
    "SandboxViolationEvent",
    "SandboxCleanupCompleted",
    "AgentUnhealthy",
    "AgentRecovered",
    "WorkflowStalled",
    "DeadLetterAppended",
    "DeadLetterRecovered",
    "CircuitBreakerTripped",
    "MouseMoved",
    "MouseClicked",
    "Scrolled",
    "TextTyped",
    "GazeFixated",
    "ReadingProgress",
    "AgentJoinedSwarm",
    "AgentLeftSwarm",
    "HeartbeatReceived",
    "HeartbeatMissed",
    "LeaderElected",
    "TaskDelegated",
    "TaskCompleted",
    "NodePartitioned",
    "PlanCreated",
    "StepPlanned",
    "ReplanTriggered",
    "PlanAdapted",
    "StepExecuted",
    "RiskEscalated",
    "EntityDiscovered",
    "RelationCreated",
    "GraphUpdated",
    "QueryExecuted",
    "InferenceFired",
    "EntityMerged",
    "PluginDiscovered",
    "PluginInstalled",
    "PluginInstallFailed",
    "NodeJoined",
    "NodeLeft",
    "MetricRecorded",
    "TraceSpanStarted",
    "TraceSpanFinished",
    "LogEntryEmitted",
    "PermissionDenied",
    "ResourceLimitExceeded",
    "PluginSandboxed",
    "AuditLogEntry",
    "McpConnected",
    "McpToolCalled",
    "McpResourceRead",
    "McpSessionClosed",
    "McpError",
]
