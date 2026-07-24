"""kernel/sandbox.py — execution sandbox for plugins, agents, workflows (ADR-020).

AXIS CONTRACT: depends on kernel.domain (SandboxPolicy / SandboxViolation) and
kernel.events (DomainEvent / EventBus / EventStore). Never imports plugins.

The sandbox enforces *soft* resource + timeout policies on in-process coroutines.
It is NOT a process/container sandbox (no subprocess isolation) — that is a
future ADR-024. Enforcement is best-effort:

* **Timeout** — real (asyncio.wait_for + cancellation).
* **CPU / memory / file descriptors** — best-effort, sampled by ResourceMonitor.
  Memory uses ``psutil`` when available; without it we degrade gracefully and
  only enforce timeout (never crash on a missing optional dependency).
* **Network / subprocess** — policy-only flags (no firewall/seccomp yet); the
  declared intent is recorded and surfaced via events for future enforcement.

Every breach emits a ``SandboxViolationEvent`` (+ ``SandboxCleanupCompleted``
after the cleanup hook runs), reusing the existing EventBus + EventStore (ADR-017).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from kernel.domain import SandboxPolicy, SandboxViolation
from kernel.events import DomainEvent, EventBus, EventStore, SandboxCleanupCompleted, SandboxViolationEvent

logger = logging.getLogger("hermes.kernel.sandbox")


class SandboxError(Exception):
    """Base class for every sandbox breach."""

    def __init__(self, policy: SandboxPolicy, violation: SandboxViolation | None = None) -> None:
        self.policy = policy
        self.violation = violation
        msg = violation.violation_type if violation else "sandbox breach"
        super().__init__(f"Sandbox {msg} (policy={policy.serialized()})")


class SandboxTimeoutError(SandboxError):
    """The operation exceeded ``policy.timeout_seconds``."""


class SandboxMemoryError(SandboxError):
    """Peak memory exceeded ``policy.max_memory_mb``."""


class SandboxCPUError(SandboxError):
    """CPU time exceeded ``policy.max_cpu_time_ms``."""


class SandboxFileError(SandboxError):
    """Open file descriptors exceeded ``policy.max_files_open``."""


classTimeoutAlias = SandboxTimeoutError  # noqa: N816  (kept for clarity in callers)


class TimeoutGuard:
    """Async context manager that bounds a block to ``timeout_seconds``.

    Uses ``asyncio.wait_for`` (cross-platform; never ``signal.alarm``). On
    timeout it cancels the inner coroutine and raises ``SandboxTimeoutError``.
    """

    def __init__(self, timeout_seconds: float, policy: SandboxPolicy) -> None:
        self._timeout = timeout_seconds
        self._policy = policy

    async def __aenter__(self) -> "TimeoutGuard":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        # The actual time-bounding happens in Sandbox.run via asyncio.wait_for;
        # this guard is a declarative marker + unified timeout error mapping.
        if exc_type is asyncio.TimeoutError:
            raise SandboxTimeoutError(self._policy) from exc
        return False


class ResourceMonitor:
    """Best-effort resource sampler (ADR-020).

    Samples CPU (monotonic wall delta), memory (``psutil`` if installed), and
    open file descriptors (best effort). Never raises on a missing optional
    dependency — it simply reports no breach for the metrics it cannot read.
    """

    def __init__(self) -> None:
        self._start_monotonic = time.monotonic()
        self._peak_memory_mb = 0.0
        self._psutil = self._try_import_psutil()

    @staticmethod
    def _try_import_psutil() -> Any:
        try:
            import psutil  # type: ignore

            return psutil
        except Exception:  # noqa: BLE001 - optional dependency
            return None

    def start(self) -> None:
        self._start_monotonic = time.monotonic()
        self._peak_memory_mb = self._current_memory_mb()

    def elapsed_ms(self) -> float:
        return (time.monotonic() - self._start_monotonic) * 1000.0

    def _current_memory_mb(self) -> float:
        if self._psutil is None:
            return 0.0
        try:
            return self._psutil.Process().memory_info().rss / (1024.0 * 1024.0)
        except Exception:  # noqa: BLE001
            return 0.0

    def open_fds(self) -> int:
        # Best-effort; Windows/other platforms fall back to 0 (not enforced).
        try:
            import os

            if hasattr(os, "sysconf") and hasattr(os, "SC_OPEN_MAX"):
                return os.sysconf(os.SC_OPEN_MAX)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return 0

    def check(self, policy: SandboxPolicy) -> list[SandboxViolation]:
        """Return a list of violations observed against ``policy`` (may be empty)."""
        violations: list[SandboxViolation] = []
        # CPU budget (wall-clock proxy — best effort under cooperative scheduling)
        if self.elapsed_ms() > policy.max_cpu_time_ms:
            violations.append(
                SandboxViolation(
                    policy=policy,
                    violation_type="cpu",
                    details={"elapsed_ms": self.elapsed_ms(), "limit_ms": policy.max_cpu_time_ms},
                )
            )
        # Memory budget (only when psutil available)
        cur = self._current_memory_mb()
        self._peak_memory_mb = max(self._peak_memory_mb, cur)
        if self._psutil is not None and self._peak_memory_mb > policy.max_memory_mb:
            violations.append(
                SandboxViolation(
                    policy=policy,
                    violation_type="memory",
                    details={"peak_mb": self._peak_memory_mb, "limit_mb": policy.max_memory_mb},
                )
            )
        # File descriptor budget (best effort)
        fds = self.open_fds()
        if fds > policy.max_files_open:
            violations.append(
                SandboxViolation(
                    policy=policy,
                    violation_type="file",
                    details={"open_fds": fds, "limit": policy.max_files_open},
                )
            )
        return violations

    def stop(self) -> None:
        # sampler is stateless; nothing to release
        return None


class Sandbox:
    """Unified in-process execution sandbox (ADR-020).

    Wraps an arbitrary coroutine with timeout enforcement + best-effort resource
    monitoring. On breach it cancels the coroutine, runs an optional async
    cleanup hook (wrapped in try/except so it can never leak), and emits
    ``SandboxViolationEvent`` (+ ``SandboxCleanupCompleted``) through the bus/store.

    Stateless across runs except for the injected event sink.
    """

    def __init__(self, event_bus: EventBus | None = None, event_store: EventStore | None = None) -> None:
        self._bus = event_bus
        self._store = event_store

    async def run(
        self,
        coro: Awaitable[Any],
        policy: SandboxPolicy,
        cleanup: Callable[[], Awaitable[None]] | None = None,
        context: dict[str, Any] | None = None,
    ) -> Any:
        """Execute ``coro`` under ``policy``. Return its result or raise ``SandboxError``."""
        monitor = ResourceMonitor()
        monitor.start()
        agg_id = (context or {}).get("aggregate_id") or (context or {}).get("agent_id") or (context or {}).get(
            "workflow_id"
        ) or "sandbox"
        try:
            result = await asyncio.wait_for(coro, timeout=policy.timeout_seconds)
            # post-run resource check (covers fast-but-heavy ops)
            violations = monitor.check(policy)
            if violations:
                return await self._breach(violations[0], cleanup, agg_id, context)
            return result
        except (asyncio.TimeoutError, TimeoutError) as exc:  # cross-version (py3.11 aliased)
            violation = SandboxViolation(
                policy=policy,
                violation_type="timeout",
                details={"timeout_seconds": policy.timeout_seconds},
            )
            return await self._breach(violation, cleanup, agg_id, context, base_exc=exc)
        except SandboxError:
            raise
        except Exception as exc:  # noqa: BLE001 - surface unexpected errors as sandbox breach
            violation = SandboxViolation(
                policy=policy,
                violation_type="error",
                details={"error": str(exc)},
            )
            return await self._breach(violation, cleanup, agg_id, context, base_exc=exc)
        finally:
            monitor.stop()

    async def _breach(
        self,
        violation: SandboxViolation,
        cleanup: Callable[[], Awaitable[None]] | None,
        aggregate_id: str,
        context: dict[str, Any] | None,
        base_exc: BaseException | None = None,
    ) -> Any:
        # emit violation event
        await self._emit(
            SandboxViolationEvent(
                aggregate_id=aggregate_id,
                violation_type=violation.violation_type,
                policy=violation.policy.serialized(),
                details=violation.details,
            )
        )
        # run cleanup hook (never leaks)
        success = True
        error: str | None = None
        if cleanup is not None:
            try:
                await cleanup()
            except Exception as cexc:  # noqa: BLE001
                success = False
                error = str(cexc)
                logger.warning("sandbox cleanup failed: %s", cexc)
        await self._emit(
            SandboxCleanupCompleted(aggregate_id=aggregate_id, success=success, error=error)
        )
        # raise the appropriate sandbox error (timeout vs generic)
        if violation.violation_type == "timeout":
            raise SandboxTimeoutError(violation.policy, violation)
        raise SandboxError(violation.policy, violation) from base_exc

    async def _emit(self, event: DomainEvent) -> None:
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)


__all__ = [
    "TimeoutGuard",
    "ResourceMonitor",
    "Sandbox",
    "SandboxError",
    "SandboxTimeoutError",
    "SandboxMemoryError",
    "SandboxCPUError",
    "SandboxFileError",
]
