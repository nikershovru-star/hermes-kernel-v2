"""kernel/resilience_domain.py — Resilience Platform domain models (ADR-031).

Isolated from ``kernel.domain`` on purpose (mirrors ``config_domain.py`` /
``security_domain.py`` / ``observability_domain.py``). ADR-031 is the *execution
resilience* layer (circuit breaker / retry / dead-letter for external MCP,
plugin and agent calls) and is deliberately DISTINCT from ADR-021 *health
recovery* (``kernel/health.py``: DistributedHealthMonitor / RecoveryEngine).

To avoid clashing with the pre-existing ADR-021 ``RetryPolicy`` /
``DeadLetterEntry`` in ``kernel.domain`` (different shapes, different layer),
every ADR-031 model is prefixed ``Resilience*`` (or uniquely named) and is NOT
exported from ``kernel/__init__.py`` — callers import from
``kernel.resilience_domain`` explicitly.

AXIS: this module imports nothing from ``kernel`` (self-contained leaf).

Honest limitations (see ADR-031):
  * Circuit state is in-memory per node — no distributed consensus.
  * Backoff is deterministic exponential, WITHOUT jitter.
  * DLQ replay is manual/triggered, not an automatic poller.
  * This is resilience, not isolation — it does NOT replace ADR-020 sandbox.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class CircuitState(str, Enum):
    """Lifecycle state of a circuit breaker.

    CLOSED    → normal operation, calls pass through, failures counted.
    OPEN      → failing fast; calls rejected immediately until recovery timeout.
    HALF_OPEN → probationary; a limited number of trial calls are allowed. A
                success closes the circuit; a failure re-opens it.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ResilienceCircuitConfig(BaseModel):
    """Circuit breaker configuration (ADR-031).

    Distinct from ADR-021 ``domain.CircuitBreakerPolicy`` — this one is keyed by
    ``name`` and drives the per-call ResilienceEngine circuit, using explicit
    millisecond timeouts and a HALF_OPEN trial-call budget.
    """

    name: str
    failure_threshold: int = 5  # consecutive failures in CLOSED before OPEN
    recovery_timeout_ms: int = 60_000  # OPEN → HALF_OPEN wait
    half_open_max_calls: int = 1  # trial calls allowed while HALF_OPEN


class ResilienceRetryPolicy(BaseModel):
    """Retry configuration for a single resilient call (ADR-031).

    Distinct from ADR-021 ``domain.RetryPolicy`` (which uses ``backoff_seconds``
    + ``exponential`` for WorkflowStep). This one uses millisecond backoff with
    an explicit ceiling and a retryable-exception allow-list (by class name).
    """

    max_attempts: int = 3
    backoff_base_ms: int = 100
    max_backoff_ms: int = 10_000
    retryable_exceptions: list[str] = Field(default_factory=list)

    def backoff_for(self, attempt: int) -> int:
        """Deterministic exponential backoff (no jitter) for ``attempt`` (1-based).

        attempt 1 → base, attempt 2 → base*2, attempt 3 → base*4, … capped at
        ``max_backoff_ms``. Returns milliseconds.
        """
        if attempt < 1:
            attempt = 1
        raw = self.backoff_base_ms * (2 ** (attempt - 1))
        return min(raw, self.max_backoff_ms)

    def is_retryable(self, exc: BaseException) -> bool:
        """True if ``exc``'s class name is in the allow-list (empty = retry all)."""
        if not self.retryable_exceptions:
            return True
        names = {type(exc).__name__}
        names.update(base.__name__ for base in type(exc).__mro__)
        return bool(names & set(self.retryable_exceptions))


class ResilienceDeadLetterEntry(BaseModel):
    """A failed task parked for later replay/analysis (ADR-031).

    Distinct from ADR-021 ``domain.DeadLetterEntry`` (component_id / entry_type /
    payload / sandbox_violation). This one captures the original task dict, the
    terminal error, the attempt count, and a replay lifecycle status.
    """

    entry_id: str
    original_task: dict
    error: str
    attempts: int = 0
    enqueued_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_attempt: datetime | None = None
    status: str = "pending"  # "pending" | "replayed" | "discarded"


class CircuitBreakerOpenError(RuntimeError):
    """Raised when a call is rejected because its circuit is OPEN (ADR-031)."""

    def __init__(self, circuit_name: str) -> None:
        self.circuit_name = circuit_name
        super().__init__(f"circuit breaker open: {circuit_name}")


class RetryExhaustedError(RuntimeError):
    """Raised when all retry attempts are exhausted (ADR-031)."""

    def __init__(self, task_id: str | None, total_attempts: int, last_error: str) -> None:
        self.task_id = task_id
        self.total_attempts = total_attempts
        self.last_error = last_error
        super().__init__(
            f"retry exhausted after {total_attempts} attempts"
            + (f" for {task_id}" if task_id else "")
            + f": {last_error}"
        )
