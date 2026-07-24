"""tests/test_sandbox.py — Sandbox core (ADR-020).

Covers TimeoutGuard, ResourceMonitor, Sandbox.run success/breach/cleanup,
and SandboxViolationEvent emission. psutil is optional; tests must pass with
or without it (graceful degradation).
"""

from __future__ import annotations

import asyncio

import pytest
from kernel.bus import EventBus
from kernel.domain import SandboxPolicy, SandboxViolation
from kernel.events import EventStore, SandboxCleanupCompleted, SandboxViolationEvent
from kernel.sandbox import (
    ResourceMonitor,
    Sandbox,
    SandboxError,
    SandboxTimeoutError,
)


async def _slow() -> str:
    await asyncio.sleep(100)
    return "never"


async def _ok() -> str:
    await asyncio.sleep(0.01)
    return "done"


@pytest.mark.asyncio
async def test_timeout_guard_raises_on_slow_coroutine() -> None:
    policy = SandboxPolicy(timeout_seconds=0.05)
    sandbox = Sandbox()
    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_slow(), policy=policy)


@pytest.mark.asyncio
async def test_sandbox_success_returns_result() -> None:
    policy = SandboxPolicy(timeout_seconds=5.0)
    sandbox = Sandbox()
    result = await sandbox.run(_ok(), policy=policy)
    assert result == "done"


@pytest.mark.asyncio
async def test_sandbox_cleanup_called_on_timeout() -> None:
    policy = SandboxPolicy(timeout_seconds=0.05)
    sandbox = Sandbox()
    cleaned: list[bool] = []

    async def _cleanup() -> None:
        cleaned.append(True)

    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_slow(), policy=policy, cleanup=_cleanup)
    assert cleaned == [True]


@pytest.mark.asyncio
async def test_sandbox_emits_violation_event() -> None:
    bus = EventBus()
    store = EventStore()
    policy = SandboxPolicy(timeout_seconds=0.05)
    sandbox = Sandbox(event_bus=bus, event_store=store)
    captured: list = []

    bus.subscribe("sandbox.violation", lambda e: captured.append(e))

    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_slow(), policy=policy)

    await asyncio.sleep(0.02)  # let bus deliver
    assert any(isinstance(e, SandboxViolationEvent) for e in captured)
    # store also received it
    assert store.count() >= 1
    types = {e.type for e in store._events}
    assert "sandbox.violation" in types


@pytest.mark.asyncio
async def test_sandbox_emits_cleanup_completed_event() -> None:
    bus = EventBus()
    store = EventStore()
    policy = SandboxPolicy(timeout_seconds=0.05)
    sandbox = Sandbox(event_bus=bus, event_store=store)
    captured: list = []
    bus.subscribe("sandbox.cleanup_completed", lambda e: captured.append(e))

    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_slow(), policy=policy)

    await asyncio.sleep(0.02)
    assert any(isinstance(e, SandboxCleanupCompleted) for e in captured)


@pytest.mark.asyncio
async def test_sandbox_cleanup_failure_does_not_leak() -> None:
    policy = SandboxPolicy(timeout_seconds=0.05)
    sandbox = Sandbox()

    async def _bad_cleanup() -> None:
        raise RuntimeError("cleanup boom")

    # breach + bad cleanup -> still raises SandboxTimeoutError, no secondary leak
    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_slow(), policy=policy, cleanup=_bad_cleanup)


@pytest.mark.asyncio
async def test_resource_monitor_graceful_without_psutil() -> None:
    mon = ResourceMonitor()
    mon.start()
    policy = SandboxPolicy(max_memory_mb=1)  # would breach if psutil present
    # without psutil, check returns no violations (degraded, never crashes)
    violations = mon.check(policy)
    assert isinstance(violations, list)  # no exception
    mon.stop()


@pytest.mark.asyncio
async def test_resource_monitor_cpu_breach_detected() -> None:
    mon = ResourceMonitor()
    mon.start()
    # force elapsed beyond cpu budget
    mon._start_monotonic = mon._start_monotonic - 100.0  # 100s ago
    policy = SandboxPolicy(max_cpu_time_ms=10)
    violations = mon.check(policy)
    assert any(v.violation_type == "cpu" for v in violations)


@pytest.mark.asyncio
async def test_sandbox_custom_policy_applied() -> None:
    # very short timeout -> even a fast coroutine that sleeps slightly breaches
    policy = SandboxPolicy(timeout_seconds=0.001)
    sandbox = Sandbox()

    async def _tiny() -> str:
        await asyncio.sleep(0.02)
        return "x"

    with pytest.raises(SandboxTimeoutError):
        await sandbox.run(_tiny(), policy=policy)
