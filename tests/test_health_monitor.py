"""tests/test_health_monitor.py — HealthMonitor probe scheduling + transitions (ADR-021).

Covers: register/unregister, check_now, consecutive-failure → UNHEALTHY,
consecutive-success → HEALTHY, DEGRADED intermediate, event emission on
transition, list_unhealthy, and background probe loop with mocked sleep.
"""

from __future__ import annotations

import asyncio

import pytest
from kernel.bus import EventBus
from kernel.domain import HealthCheck, HealthStatus
from kernel.events import EventStore
from kernel.health import HealthMonitor


def _monitor() -> tuple[HealthMonitor, EventBus, EventStore]:
    bus = EventBus()
    store = EventStore()
    return HealthMonitor(bus, store), bus, store


async def test_unknown_before_first_probe() -> None:
    mon, _, _ = _monitor()
    mon.register("a1", "agent", probe=lambda: _true(), check=HealthCheck())
    assert mon.get_status("a1") == HealthStatus.UNKNOWN


async def test_check_now_healthy() -> None:
    mon, _, _ = _monitor()
    mon.register("a1", "agent", probe=lambda: _true(), check=HealthCheck(success_threshold=1))
    rec = await mon.check_now("a1")
    assert rec.status == HealthStatus.HEALTHY
    assert rec.consecutive_successes == 1


async def test_consecutive_failures_unhealthy_and_event() -> None:
    mon, _, store = _monitor()
    mon.register("a1", "agent", probe=lambda: _false(), check=HealthCheck(failure_threshold=3))
    await mon.check_now("a1")  # DEGRADED
    assert mon.get_status("a1") == HealthStatus.DEGRADED
    await mon.check_now("a1")
    rec = await mon.check_now("a1")  # 3rd → UNHEALTHY
    assert rec.status == HealthStatus.UNHEALTHY
    events = await store.read_stream("a1")
    assert any(e.type == "health.agent_unhealthy" for e in events)
    assert "a1" in mon.list_unhealthy()


async def test_recovery_emits_recovered_event() -> None:
    mon, _, store = _monitor()
    flip = {"healthy": False}

    async def probe() -> bool:
        return flip["healthy"]

    mon.register("a1", "agent", probe=probe, check=HealthCheck(failure_threshold=2, success_threshold=1))
    await mon.check_now("a1")
    await mon.check_now("a1")  # UNHEALTHY
    assert mon.get_status("a1") == HealthStatus.UNHEALTHY
    flip["healthy"] = True
    rec = await mon.check_now("a1")  # HEALTHY again
    assert rec.status == HealthStatus.HEALTHY
    events = await store.read_stream("a1")
    assert any(e.type == "health.agent_recovered" for e in events)


async def test_probe_exception_counts_as_failure() -> None:
    mon, _, _ = _monitor()

    async def boom() -> bool:
        raise RuntimeError("probe blew up")

    mon.register("a1", "agent", probe=boom, check=HealthCheck(failure_threshold=1))
    rec = await mon.check_now("a1")
    assert rec.status == HealthStatus.UNHEALTHY
    assert rec.last_error is not None


async def test_probe_timeout_counts_as_failure() -> None:
    mon, _, _ = _monitor()

    async def slow() -> bool:
        await asyncio.sleep(10)
        return True

    mon.register("a1", "agent", probe=slow, check=HealthCheck(failure_threshold=1, timeout_seconds=0.05))
    rec = await mon.check_now("a1")
    assert rec.status == HealthStatus.UNHEALTHY


async def test_unregister_removes_component() -> None:
    mon, _, _ = _monitor()
    mon.register("a1", "agent", probe=lambda: _true(), check=HealthCheck())
    mon.unregister("a1")
    assert mon.get_status("a1") == HealthStatus.UNKNOWN
    with pytest.raises(KeyError):
        await mon.check_now("a1")


async def test_check_now_unknown_component_raises() -> None:
    mon, _, _ = _monitor()
    with pytest.raises(KeyError):
        await mon.check_now("nope")


async def test_background_loop_probes_and_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    mon, _, _ = _monitor()
    calls = {"n": 0}

    async def probe() -> bool:
        calls["n"] += 1
        return True

    # Make sleep instant so the loop iterates fast.
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("kernel.health.asyncio.sleep", fast_sleep)
    mon.register("a1", "agent", probe=probe, check=HealthCheck(interval_seconds=0.01, success_threshold=1))
    await mon.start()
    await real_sleep(0.05)
    await mon.stop()
    assert calls["n"] >= 1
    assert mon.get_status("a1") == HealthStatus.HEALTHY


async def _true() -> bool:
    return True


async def _false() -> bool:
    return False
