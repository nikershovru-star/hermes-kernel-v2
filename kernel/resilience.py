"""kernel/resilience.py — Resilience Platform engine (ADR-031).

``ResilienceEngine`` provides execution-resilience primitives for calls to
external MCP servers, plugins and agents:

  * **Circuit breaker** — per-name CLOSED → OPEN → HALF_OPEN → CLOSED state
    machine, failing fast while OPEN.
  * **Retry** — deterministic exponential backoff (no jitter) with a retryable
    exception allow-list and injectable ``sleep``.
  * **Dead-letter queue** — parks terminally-failed tasks for manual replay.

Everything is async + fully injectable (``store`` / ``event_bus`` /
``event_store`` / ``clock`` / ``sleep`` / ``metrics``) for deterministic tests.

AXIS: imports only ``kernel.resilience_domain`` + ``kernel.events`` (+ typing).
It never imports the consumer engines (agent / workflow / mcp_gateway) — they
inject an instance. Distinct from ADR-021 ``kernel/health.py`` (health
recovery); this is the execution-resilience layer.

Honest limitations (see ADR-031):
  * Circuit state is in-memory per node — no distributed consensus.
  * Backoff is deterministic exponential, WITHOUT jitter.
  * DLQ replay is manual/triggered, not an automatic poller.
  * Resilience, not isolation — does NOT replace the ADR-020 sandbox.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.events import (
    CircuitBreakerClosed,
    CircuitBreakerOpened,
    DeadLetterEnqueued,
    EventBus,
    EventStore,
    RetryAttempted,
    RetryExhausted,
)
from kernel.resilience_domain import (
    CircuitBreakerOpenError,
    CircuitState,
    ResilienceCircuitConfig,
    ResilienceDeadLetterEntry,
    ResilienceRetryPolicy,
    RetryExhaustedError,
)


async def _default_sleep(_seconds: float) -> None:  # pragma: no cover - trivial
    return None


class _CircuitRuntime:
    """Mutable per-circuit runtime state (in-memory)."""

    __slots__ = ("config", "state", "failure_count", "last_failure", "half_open_calls")

    def __init__(self, config: ResilienceCircuitConfig) -> None:
        self.config = config
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_failure: datetime | None = None
        self.half_open_calls = 0


class ResilienceEngine:
    """Circuit breaker + retry + dead-letter engine (ADR-031)."""

    def __init__(
        self,
        store: Any | None = None,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        metrics: Any | None = None,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._event_store = event_store
        self._clock = clock
        self._sleep = sleep
        self._metrics = metrics
        self._circuits: dict[str, _CircuitRuntime] = {}
        self._dlq: dict[str, ResilienceDeadLetterEntry] = {}

    # -- emit helpers ---------------------------------------------------- #
    async def _emit(self, event) -> None:
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001 - persistence never breaks a call
                pass
        if self._bus is not None:
            self._bus.publish(event)

    async def _metric(self, name: str, value: float, labels: dict[str, str] | None = None) -> None:
        if self._metrics is None:
            return
        try:
            await self._metrics.record_metric(name, value, labels=labels or {})
        except Exception:  # noqa: BLE001 - metrics never break a call
            pass

    # -- circuit breaker ------------------------------------------------- #
    def register_circuit(self, name: str, config: ResilienceCircuitConfig | None = None) -> None:
        """Create (or replace) a circuit breaker keyed by ``name``."""
        cfg = config or ResilienceCircuitConfig(name=name)
        # keep name authoritative even if a mismatched config is passed
        if cfg.name != name:
            cfg = cfg.model_copy(update={"name": name})
        self._circuits[name] = _CircuitRuntime(cfg)
        if self._store is not None:
            self._store.put_circuit(name, CircuitState.CLOSED.value, 0, None, cfg.model_dump_json())

    def get_circuit_status(self, name: str) -> CircuitState:
        """Return the current ``CircuitState`` for ``name`` (auto-transitions
        OPEN → HALF_OPEN when the recovery timeout has elapsed)."""
        rt = self._circuits.get(name)
        if rt is None:
            raise KeyError(f"circuit not registered: {name}")
        self._maybe_recover(rt)
        return rt.state

    def _maybe_recover(self, rt: _CircuitRuntime) -> None:
        """OPEN → HALF_OPEN once ``recovery_timeout_ms`` has elapsed."""
        if rt.state is not CircuitState.OPEN or rt.last_failure is None:
            return
        elapsed_ms = (self._clock() - rt.last_failure).total_seconds() * 1000.0
        if elapsed_ms >= rt.config.recovery_timeout_ms:
            rt.state = CircuitState.HALF_OPEN
            rt.half_open_calls = 0

    async def _on_success(self, rt: _CircuitRuntime) -> None:
        if rt.state is CircuitState.HALF_OPEN:
            # a trial call succeeded → close the circuit
            rt.state = CircuitState.CLOSED
            rt.failure_count = 0
            rt.half_open_calls = 0
            await self._persist(rt)
            await self._emit(CircuitBreakerClosed(rt.config.name, from_state="half_open"))
            await self._metric("res.circuit_closed", 1.0, {"circuit": rt.config.name})
        else:
            rt.failure_count = 0

    async def _on_failure(self, rt: _CircuitRuntime) -> None:
        rt.last_failure = self._clock()
        if rt.state is CircuitState.HALF_OPEN:
            # trial failed → re-open immediately
            rt.state = CircuitState.OPEN
            rt.half_open_calls = 0
            await self._persist(rt)
            await self._emit(CircuitBreakerOpened(rt.config.name, rt.failure_count, rt.config.failure_threshold))
            await self._metric("res.circuit_opened", 1.0, {"circuit": rt.config.name})
            return
        rt.failure_count += 1
        if rt.failure_count >= rt.config.failure_threshold and rt.state is CircuitState.CLOSED:
            rt.state = CircuitState.OPEN
            await self._persist(rt)
            await self._emit(CircuitBreakerOpened(rt.config.name, rt.failure_count, rt.config.failure_threshold))
            await self._metric("res.circuit_opened", 1.0, {"circuit": rt.config.name})

    async def _persist(self, rt: _CircuitRuntime) -> None:
        if self._store is not None:
            self._store.put_circuit(
                rt.config.name, rt.state.value, rt.failure_count,
                rt.last_failure, rt.config.model_dump_json(),
            )

    @asynccontextmanager
    async def call_with_circuit(self, name: str):
        """Async context manager guarding a call with circuit breaker ``name``.

        Auto-registers a default circuit if ``name`` is unknown. Raises
        ``CircuitBreakerOpenError`` immediately when the circuit is OPEN (and the
        recovery timeout has not elapsed). On exit, records success/failure and
        drives the state machine. Usage::

            async with engine.call_with_circuit("mcp:srv"):
                result = await do_call()
        """
        rt = self._circuits.get(name)
        if rt is None:
            self.register_circuit(name)
            rt = self._circuits[name]
        self._maybe_recover(rt)
        if rt.state is CircuitState.OPEN:
            await self._metric("res.circuit_rejected", 1.0, {"circuit": name})
            raise CircuitBreakerOpenError(name)
        if rt.state is CircuitState.HALF_OPEN:
            if rt.half_open_calls >= rt.config.half_open_max_calls:
                await self._metric("res.circuit_rejected", 1.0, {"circuit": name})
                raise CircuitBreakerOpenError(name)
            rt.half_open_calls += 1
        try:
            yield
        except Exception:
            await self._on_failure(rt)
            raise
        else:
            await self._on_success(rt)

    # -- retry ----------------------------------------------------------- #
    async def retry(
        self,
        coro_factory: Callable[[], Awaitable[Any]],
        policy: ResilienceRetryPolicy | None = None,
        task_id: str | None = None,
    ) -> Any:
        """Execute ``coro_factory()`` with retry + deterministic backoff.

        ``coro_factory`` MUST be a zero-arg callable returning a fresh awaitable
        each attempt (a bare coroutine can only be awaited once). Retries only
        exceptions allowed by ``policy.is_retryable``. Emits ``RetryAttempted``
        per retry; on exhaustion emits ``RetryExhausted`` and raises
        ``RetryExhaustedError``.
        """
        pol = policy or ResilienceRetryPolicy()
        tid = task_id or uuid.uuid4().hex
        last_error = ""
        for attempt in range(1, pol.max_attempts + 1):
            try:
                return await coro_factory()
            except Exception as exc:  # noqa: BLE001 - engine decides retryability
                last_error = f"{type(exc).__name__}: {exc}"
                if not pol.is_retryable(exc) or attempt >= pol.max_attempts:
                    await self._emit(RetryExhausted(tid, attempt, last_error))
                    await self._metric("res.retry_exhausted", 1.0, {"task": tid})
                    raise RetryExhaustedError(task_id, attempt, last_error) from exc
                backoff_ms = pol.backoff_for(attempt)
                await self._emit(RetryAttempted(tid, attempt, backoff_ms, last_error))
                await self._metric("res.retry_attempted", 1.0, {"task": tid})
                if self._store is not None:
                    self._store.put_retry(tid, attempt, backoff_ms, last_error, self._clock())
                await self._sleep(backoff_ms / 1000.0)
        # unreachable, but keeps type-checkers happy
        raise RetryExhaustedError(task_id, pol.max_attempts, last_error)

    # -- dead-letter queue ---------------------------------------------- #
    async def enqueue_dead_letter(
        self, task: dict, error: str, attempts: int, entry_id: str | None = None
    ) -> ResilienceDeadLetterEntry:
        """Park a terminally-failed ``task`` in the DLQ; emit DeadLetterEnqueued."""
        eid = entry_id or uuid.uuid4().hex
        entry = ResilienceDeadLetterEntry(
            entry_id=eid,
            original_task=dict(task),
            error=error,
            attempts=attempts,
            enqueued_at=self._clock(),
            status="pending",
        )
        self._dlq[eid] = entry
        if self._store is not None:
            self._store.put_dead_letter(entry)
        await self._emit(DeadLetterEnqueued(eid, error, attempts))
        await self._metric("res.dead_letter_enqueued", 1.0, {})
        return entry

    async def replay_dead_letter(
        self, entry_id: str, coro_factory: Callable[[dict], Awaitable[Any]]
    ) -> Any:
        """Replay a DLQ entry via ``coro_factory(original_task)``.

        On success the entry status becomes ``replayed``; on failure it stays
        ``pending`` (with ``last_attempt`` bumped) and the exception propagates.
        """
        entry = self._dlq.get(entry_id)
        if entry is None and self._store is not None:
            entry = self._store.get_dead_letter(entry_id)
        if entry is None:
            raise KeyError(f"dead-letter entry not found: {entry_id}")
        entry.last_attempt = self._clock()
        try:
            result = await coro_factory(entry.original_task)
        except Exception:
            entry.attempts += 1
            self._dlq[entry_id] = entry
            if self._store is not None:
                self._store.update_dead_letter_status(entry_id, "pending", entry.last_attempt)
                self._store.put_dead_letter(entry)
            raise
        entry.status = "replayed"
        self._dlq[entry_id] = entry
        if self._store is not None:
            self._store.update_dead_letter_status(entry_id, "replayed", entry.last_attempt)
        return result

    def discard_dead_letter(self, entry_id: str) -> bool:
        """Mark a DLQ entry ``discarded`` (won't appear in pending list)."""
        entry = self._dlq.get(entry_id)
        if entry is None and self._store is not None:
            entry = self._store.get_dead_letter(entry_id)
        if entry is None:
            return False
        entry.status = "discarded"
        self._dlq[entry_id] = entry
        if self._store is not None:
            self._store.update_dead_letter_status(entry_id, "discarded", self._clock())
        return True

    def list_dead_letter(self, status: str = "pending") -> list[ResilienceDeadLetterEntry]:
        """List DLQ entries filtered by ``status`` (None → all)."""
        if self._store is not None:
            return self._store.list_dead_letter(status)
        entries = list(self._dlq.values())
        if status is not None:
            entries = [e for e in entries if e.status == status]
        return entries
