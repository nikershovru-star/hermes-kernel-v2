"""kernel/health.py — Health & Recovery layer for the v5 Execution Platform (ADR-021).

AXIS CONTRACT: depends on kernel.domain (Health*/DeadLetter*/CircuitBreaker*),
kernel.events (DomainEvent / EventBus / EventStore + health events), and — for
RecoveryEngine only — kernel.agent (AgentRuntime) + kernel.workflow
(WorkflowEngine) via *duck typing* (no import-time dependency on them; they are
injected). Never imports plugins.

This module provides four cooperating primitives:

* ``HealthMonitor``   — periodic liveness probes → HealthRecord per component,
  emits AgentUnhealthy / AgentRecovered on status transitions.
* ``DeadLetterQueue`` — append-only store of failed work for replay/analysis.
* ``CircuitBreaker``  — per-capability CLOSED→OPEN→HALF_OPEN state machine.
* ``RecoveryEngine``  — decides what to do when a component becomes unhealthy
  (restart agent / compensate workflow / dead-letter + escalate).

Enforcement is in-process and single-node (no external probe endpoint, no
distributed health) — see ADR-021 honest notes.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, TypeVar

from kernel.domain import (
    CircuitBreakerPolicy,
    CircuitBreakerState,
    DeadLetterEntry,
    HealthCheck,
    HealthRecord,
    HealthStatus,
)
from kernel.events import (
    AgentRecovered,
    AgentUnhealthy,
    CircuitBreakerTripped,
    DeadLetterAppended,
    DeadLetterRecovered,
    EventBus,
    EventStore,
)

logger = logging.getLogger("hermes.kernel.health")

T = TypeVar("T")

Probe = Callable[[], Awaitable[bool]]


class CircuitBreakerOpen(Exception):
    """Raised when a call is rejected because the breaker is OPEN."""

    def __init__(self, capability: str) -> None:
        self.capability = capability
        super().__init__(f"circuit breaker OPEN for capability {capability!r}")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# HealthMonitor
# --------------------------------------------------------------------------- #
class _Registration:
    __slots__ = ("component_type", "probe", "check", "record", "task")

    def __init__(
        self,
        component_type: str,
        probe: Probe,
        check: HealthCheck,
        record: HealthRecord,
    ) -> None:
        self.component_type = component_type
        self.probe = probe
        self.check = check
        self.record = record
        self.task: asyncio.Task[None] | None = None


class HealthMonitor:
    """Periodic health probe scheduler (ADR-021).

    Maintains a ``HealthRecord`` for every registered component. Probes run via
    one ``asyncio.Task`` per component. Emits ``AgentUnhealthy`` /
    ``AgentRecovered`` on status transitions (through EventBus + EventStore).
    """

    def __init__(self, event_bus: EventBus, event_store: EventStore) -> None:
        self._bus = event_bus
        self._store = event_store
        self._registry: dict[str, _Registration] = {}
        self._running = False

    # -- registration ----------------------------------------------------- #
    def register(
        self,
        component_id: str,
        component_type: str,
        probe: Probe,
        check: HealthCheck,
    ) -> None:
        """Register ``probe`` for ``component_id`` with ``check`` config."""
        record = HealthRecord(component_id=component_id, component_type=component_type)
        reg = _Registration(component_type, probe, check, record)
        self._registry[component_id] = reg
        # If the monitor is already running, spin up a probe loop immediately.
        if self._running and check.enabled:
            reg.task = asyncio.create_task(self._probe_loop(component_id))
        logger.debug("health: registered %s (%s)", component_id, component_type)

    def unregister(self, component_id: str) -> None:
        reg = self._registry.pop(component_id, None)
        if reg is not None and reg.task is not None:
            reg.task.cancel()
        logger.debug("health: unregistered %s", component_id)

    # -- probing ---------------------------------------------------------- #
    async def check_now(self, component_id: str) -> HealthRecord:
        """Force an immediate probe, update the record, emit on transition."""
        reg = self._registry.get(component_id)
        if reg is None:
            raise KeyError(f"no component registered: {component_id}")
        healthy = await self._run_probe(reg)
        await self._apply_result(reg, healthy)
        return reg.record

    async def _run_probe(self, reg: _Registration) -> bool:
        try:
            return await asyncio.wait_for(reg.probe(), timeout=reg.check.timeout_seconds)
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001 — probe failure = unhealthy
            reg.record.last_error = str(exc) or type(exc).__name__
            return False

    async def _apply_result(self, reg: _Registration, healthy: bool) -> None:
        rec = reg.record
        check = reg.check
        prev = rec.status
        rec.last_probe_at = _now()
        if healthy:
            rec.consecutive_successes += 1
            rec.consecutive_failures = 0
            if rec.consecutive_successes >= check.success_threshold:
                rec.status = HealthStatus.HEALTHY
                if prev != HealthStatus.HEALTHY:
                    rec.last_error = None
        else:
            rec.consecutive_failures += 1
            rec.consecutive_successes = 0
            if rec.consecutive_failures >= check.failure_threshold:
                rec.status = HealthStatus.UNHEALTHY
            elif rec.status in (HealthStatus.HEALTHY, HealthStatus.UNKNOWN):
                rec.status = HealthStatus.DEGRADED
        if rec.status != prev:
            await self._emit_transition(rec, prev)

    async def _emit_transition(self, rec: HealthRecord, prev: HealthStatus) -> None:
        if rec.status == HealthStatus.UNHEALTHY:
            event = AgentUnhealthy(
                component_id=rec.component_id,
                last_error=rec.last_error,
                consecutive_failures=rec.consecutive_failures,
            )
        elif rec.status == HealthStatus.HEALTHY and prev == HealthStatus.UNHEALTHY:
            event = AgentRecovered(component_id=rec.component_id, restart_count=0)
        else:
            return  # DEGRADED / other transitions are not externally signalled
        await self._store.append(event)
        self._bus.publish(event)

    # -- accessors -------------------------------------------------------- #
    def get_status(self, component_id: str) -> HealthStatus:
        reg = self._registry.get(component_id)
        return reg.record.status if reg else HealthStatus.UNKNOWN

    def get_record(self, component_id: str) -> HealthRecord | None:
        reg = self._registry.get(component_id)
        return reg.record if reg else None

    def list_unhealthy(self) -> list[str]:
        return [
            cid
            for cid, reg in self._registry.items()
            if reg.record.status == HealthStatus.UNHEALTHY
        ]

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        """Start one background probe loop per registered component."""
        self._running = True
        for cid, reg in self._registry.items():
            if reg.check.enabled and reg.task is None:
                reg.task = asyncio.create_task(self._probe_loop(cid))

    async def stop(self) -> None:
        """Cancel all probe tasks."""
        self._running = False
        tasks = [reg.task for reg in self._registry.values() if reg.task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        for reg in self._registry.values():
            reg.task = None

    async def _probe_loop(self, component_id: str) -> None:
        reg = self._registry.get(component_id)
        if reg is None:
            return
        try:
            while self._running:
                await asyncio.sleep(reg.check.interval_seconds)
                current = self._registry.get(component_id)
                if current is None or not self._running:
                    break
                healthy = await self._run_probe(current)
                await self._apply_result(current, healthy)
        except asyncio.CancelledError:
            raise


# --------------------------------------------------------------------------- #
# DeadLetterQueue
# --------------------------------------------------------------------------- #
class DeadLetterQueue:
    """Append-only store of failed tasks/events for replay/analysis (ADR-021).

    In-memory + optional EventStore emission. Dead letters are NOT domain events
    themselves; the *fact* of appending/recovering IS signalled via events.
    """

    def __init__(self, event_store: EventStore | None = None, event_bus: EventBus | None = None) -> None:
        self._store = event_store
        self._bus = event_bus
        self._entries: list[DeadLetterEntry] = []

    async def append(self, entry: DeadLetterEntry) -> None:
        """Store ``entry``; emit ``DeadLetterAppended`` if wired to events."""
        self._entries.append(entry)
        event = DeadLetterAppended(
            entry_id=entry.entry_id,
            component_id=entry.component_id,
            entry_type=entry.entry_type,
            retry_count=entry.retry_count,
        )
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)

    async def list(
        self, component_id: str | None = None, limit: int = 100
    ) -> list[DeadLetterEntry]:
        items = self._entries
        if component_id is not None:
            items = [e for e in items if e.component_id == component_id]
        return items[:limit]

    def get(self, entry_id: str) -> DeadLetterEntry | None:
        for e in self._entries:
            if e.entry_id == entry_id:
                return e
        return None

    async def recover(self, entry_id: str) -> DeadLetterEntry | None:
        """Mark ``entry_id`` recovered; emit ``DeadLetterRecovered``."""
        entry = self.get(entry_id)
        if entry is None:
            return None
        entry.recovered_at = _now()
        event = DeadLetterRecovered(entry_id=entry.entry_id, component_id=entry.component_id)
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)
        return entry

    async def replay(
        self,
        component_id: str,
        handler: Callable[[DeadLetterEntry], Awaitable[bool]],
    ) -> int:
        """Replay all unrecovered entries for ``component_id`` through ``handler``.

        Idempotent by design: only entries with ``recovered_at is None`` are
        replayed; ``handler`` decides success (returns True → mark recovered).
        Returns the count of successfully replayed entries.
        """
        recovered = 0
        for entry in list(self._entries):
            if entry.component_id != component_id or entry.recovered_at is not None:
                continue
            ok = await handler(entry)
            if ok:
                await self.recover(entry.entry_id)
                recovered += 1
        return recovered

    def count(self, component_id: str | None = None) -> int:
        if component_id is None:
            return len(self._entries)
        return sum(1 for e in self._entries if e.component_id == component_id)


# --------------------------------------------------------------------------- #
# CircuitBreaker
# --------------------------------------------------------------------------- #
class _BreakerState:
    __slots__ = ("state", "failure_count", "success_count", "opened_at", "half_open_inflight")

    def __init__(self) -> None:
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.success_count = 0
        self.opened_at: float | None = None
        self.half_open_inflight = False


class CircuitBreaker:
    """Per-capability circuit breaker (ADR-021).

    State machine: CLOSED → (failure_threshold failures) → OPEN →
    (recovery_timeout elapsed) → HALF_OPEN → (success_threshold successes) →
    CLOSED. HALF_OPEN admits EXACTLY ONE test call at a time.
    """

    def __init__(
        self,
        policy: CircuitBreakerPolicy | None = None,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        *,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or CircuitBreakerPolicy()
        self._bus = event_bus
        self._store = event_store
        self._states: dict[str, _BreakerState] = {}
        self._time = time_fn or (lambda: asyncio.get_event_loop().time())

    def _get(self, capability: str) -> _BreakerState:
        st = self._states.get(capability)
        if st is None:
            st = _BreakerState()
            self._states[capability] = st
        return st

    def state(self, capability: str) -> CircuitBreakerState:
        st = self._states.get(capability)
        if st is None:
            return CircuitBreakerState.CLOSED
        self._maybe_half_open(capability, st)
        return st.state

    def _maybe_half_open(self, capability: str, st: _BreakerState) -> None:
        if st.state == CircuitBreakerState.OPEN and st.opened_at is not None:
            if self._time() - st.opened_at >= self._policy.recovery_timeout_seconds:
                st.state = CircuitBreakerState.HALF_OPEN
                st.success_count = 0
                st.half_open_inflight = False

    def reset(self, capability: str) -> None:
        self._states[capability] = _BreakerState()

    @staticmethod
    def _close_coro(coro: Awaitable[Any]) -> None:
        """Close an un-awaited coroutine to avoid RuntimeWarning on rejection."""
        close = getattr(coro, "close", None)
        if callable(close):
            close()

    async def call(
        self,
        capability: str,
        coro: Awaitable[T],
        on_state_change: Callable[[str, CircuitBreakerState], None] | None = None,
    ) -> T:
        st = self._get(capability)
        self._maybe_half_open(capability, st)

        if st.state == CircuitBreakerState.OPEN:
            self._close_coro(coro)
            raise CircuitBreakerOpen(capability)

        if st.state == CircuitBreakerState.HALF_OPEN:
            if st.half_open_inflight:
                # Exactly one test call allowed while HALF_OPEN.
                self._close_coro(coro)
                raise CircuitBreakerOpen(capability)
            st.half_open_inflight = True

        try:
            result = await coro
        except Exception:
            await self._on_failure(capability, st, on_state_change)
            raise
        else:
            await self._on_success(capability, st, on_state_change)
            return result

    async def _on_success(
        self,
        capability: str,
        st: _BreakerState,
        cb: Callable[[str, CircuitBreakerState], None] | None,
    ) -> None:
        if st.state == CircuitBreakerState.HALF_OPEN:
            st.half_open_inflight = False
            st.success_count += 1
            if st.success_count >= self._policy.success_threshold:
                await self._transition(capability, st, CircuitBreakerState.CLOSED, cb)
        else:  # CLOSED
            st.failure_count = 0

    async def _on_failure(
        self,
        capability: str,
        st: _BreakerState,
        cb: Callable[[str, CircuitBreakerState], None] | None,
    ) -> None:
        if st.state == CircuitBreakerState.HALF_OPEN:
            st.half_open_inflight = False
            await self._transition(capability, st, CircuitBreakerState.OPEN, cb)
            return
        st.failure_count += 1
        if st.failure_count >= self._policy.failure_threshold:
            await self._transition(capability, st, CircuitBreakerState.OPEN, cb)

    async def _transition(
        self,
        capability: str,
        st: _BreakerState,
        new_state: CircuitBreakerState,
        cb: Callable[[str, CircuitBreakerState], None] | None,
    ) -> None:
        st.state = new_state
        if new_state == CircuitBreakerState.OPEN:
            st.opened_at = self._time()
            st.success_count = 0
        elif new_state == CircuitBreakerState.CLOSED:
            st.failure_count = 0
            st.success_count = 0
            st.opened_at = None
        event = CircuitBreakerTripped(
            capability=capability, state=new_state.value, failure_count=st.failure_count
        )
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)
        if cb is not None:
            cb(capability, new_state)


# --------------------------------------------------------------------------- #
# RecoveryEngine
# --------------------------------------------------------------------------- #
class RecoveryEngine:
    """Decides what to do when a component becomes unhealthy (ADR-021).

    Listens for ``AgentUnhealthy`` events from the ``HealthMonitor`` (via the
    ``EventBus``) and applies a recovery decision tree. Collaborators
    (``agent_runtime``, ``workflow_engine``) are injected and used via duck
    typing — no import-time dependency, so this stays axis-clean and testable
    with mocks.

    Restart is bounded by ``max_restarts`` per component; on exhaustion the work
    is appended to the ``DeadLetterQueue`` and a human escalation is logged.
    """

    def __init__(
        self,
        health_monitor: HealthMonitor,
        dead_letter: DeadLetterQueue,
        agent_runtime: Any,
        workflow_engine: Any,
        event_bus: EventBus,
        event_store: EventStore,
        *,
        max_restarts: int = 3,
    ) -> None:
        self._health = health_monitor
        self._dlq = dead_letter
        self._agents = agent_runtime
        self._workflows = workflow_engine
        self._bus = event_bus
        self._store = event_store
        self._max_restarts = max_restarts
        self._restart_counts: dict[str, int] = {}
        self._sub_id: str | None = None

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        """Subscribe to ``AgentUnhealthy`` events on the bus."""
        if self._sub_id is None:
            self._sub_id = self._bus.subscribe("health.agent_unhealthy", self._on_unhealthy_event)

    async def stop(self) -> None:
        if self._sub_id is not None:
            self._bus.unsubscribe(self._sub_id)
            self._sub_id = None

    async def _on_unhealthy_event(self, event: Any) -> None:
        component_id = getattr(event, "aggregate_id", "")
        record = self._health.get_record(component_id)
        component_type = record.component_type if record else "agent"
        await self.on_unhealthy(component_id, component_type, record)

    # -- decision tree ---------------------------------------------------- #
    async def on_unhealthy(
        self, component_id: str, component_type: str, record: HealthRecord | None
    ) -> None:
        """Apply the recovery decision for a newly-unhealthy component."""
        restarts = self._restart_counts.get(component_id, 0)
        if restarts >= self._max_restarts:
            await self._escalate(component_id, component_type, record)
            return

        if component_type == "agent":
            ok = await self._restart_agent(component_id)
        elif component_type == "workflow":
            ok = await self._recover_workflow(component_id, record)
        else:
            ok = await self._restart_agent(component_id)  # best-effort default

        if ok:
            self._restart_counts[component_id] = restarts + 1
            event = AgentRecovered(
                component_id=component_id, restart_count=self._restart_counts[component_id]
            )
            await self._store.append(event)
            self._bus.publish(event)
        else:
            await self._escalate(component_id, component_type, record)

    async def _restart_agent(self, agent_id: str) -> bool:
        try:
            agent = self._agents.get(agent_id)
            if agent is None:
                return False
            await self._agents.stop(agent_id)
            await self._agents.start(agent)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery: agent restart failed for %s: %s", agent_id, exc)
            return False

    async def _recover_workflow(self, instance_id: str, record: HealthRecord | None) -> bool:
        try:
            instance = self._workflows.get_instance(instance_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recovery: workflow %s not found: %s", instance_id, exc)
            return False
        # Dead-letter the stalled workflow for replay/analysis rather than
        # silently re-running (compensation already ran in the engine).
        entry = DeadLetterEntry(
            entry_id=str(uuid.uuid4()),
            component_id=instance_id,
            entry_type="workflow_step",
            payload={"instance_id": instance_id},
            error=(record.last_error if record else None) or "workflow stalled",
        )
        await self._dlq.append(entry)
        return True

    async def _escalate(
        self, component_id: str, component_type: str, record: HealthRecord | None
    ) -> None:
        """Max restarts exceeded → dead-letter + human-escalation (log-only)."""
        entry = DeadLetterEntry(
            entry_id=str(uuid.uuid4()),
            component_id=component_id,
            entry_type="task" if component_type == "agent" else "workflow_step",
            payload={"component_type": component_type},
            error=(record.last_error if record else None) or "max restarts exceeded",
            retry_count=self._restart_counts.get(component_id, 0),
        )
        await self._dlq.append(entry)
        logger.error(
            "recovery: ESCALATION — %s %s unrecoverable after %d restarts (dead-lettered %s)",
            component_type,
            component_id,
            self._restart_counts.get(component_id, 0),
            entry.entry_id,
        )

    async def on_dead_letter(self, entry: DeadLetterEntry) -> str:
        """Decide the fate of a dead-letter entry: retry / escalate / archive."""
        if entry.recovered_at is not None:
            return "archive"
        if entry.retry_count < entry.max_retries:
            return "retry"
        return "escalate"

    def restart_count(self, component_id: str) -> int:
        return self._restart_counts.get(component_id, 0)


__all__ = [
    "CircuitBreaker",
    "CircuitBreakerOpen",
    "DeadLetterQueue",
    "HealthMonitor",
    "Probe",
    "RecoveryEngine",
]