"""tests/test_circuit_breaker.py — CircuitBreaker state machine (ADR-021).

Covers: CLOSED → OPEN after failure_threshold, reject in OPEN, OPEN → HALF_OPEN
after recovery_timeout (mocked clock), HALF_OPEN admits exactly one test call,
HALF_OPEN success → CLOSED after success_threshold, HALF_OPEN failure → OPEN,
reset, and event emission.
"""

from __future__ import annotations

import pytest
from kernel.bus import EventBus
from kernel.domain import CircuitBreakerPolicy, CircuitBreakerState
from kernel.events import EventStore
from kernel.health import CircuitBreaker, CircuitBreakerOpen


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _cb(policy: CircuitBreakerPolicy | None = None, clock: _Clock | None = None) -> CircuitBreaker:
    return CircuitBreaker(
        policy=policy or CircuitBreakerPolicy(failure_threshold=3, recovery_timeout_seconds=60.0, success_threshold=2),
        time_fn=clock or _Clock(),
    )


async def _ok() -> str:
    return "ok"


async def _fail() -> str:
    raise RuntimeError("boom")


async def test_closed_passes_through() -> None:
    cb = _cb()
    assert await cb.call("cap", _ok()) == "ok"
    assert cb.state("cap") == CircuitBreakerState.CLOSED


async def test_opens_after_threshold() -> None:
    cb = _cb()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    assert cb.state("cap") == CircuitBreakerState.OPEN


async def test_open_rejects_immediately() -> None:
    cb = _cb()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    with pytest.raises(CircuitBreakerOpen):
        await cb.call("cap", _ok())


async def test_open_to_half_open_after_timeout() -> None:
    clock = _Clock()
    cb = _cb(clock=clock)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    assert cb.state("cap") == CircuitBreakerState.OPEN
    clock.advance(61.0)
    assert cb.state("cap") == CircuitBreakerState.HALF_OPEN


async def test_half_open_success_closes_after_threshold() -> None:
    clock = _Clock()
    cb = _cb(clock=clock)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    clock.advance(61.0)
    await cb.call("cap", _ok())  # 1st success in HALF_OPEN
    assert cb.state("cap") == CircuitBreakerState.HALF_OPEN
    await cb.call("cap", _ok())  # 2nd → CLOSED
    assert cb.state("cap") == CircuitBreakerState.CLOSED


async def test_half_open_failure_reopens() -> None:
    clock = _Clock()
    cb = _cb(clock=clock)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    clock.advance(61.0)
    assert cb.state("cap") == CircuitBreakerState.HALF_OPEN
    with pytest.raises(RuntimeError):
        await cb.call("cap", _fail())
    assert cb.state("cap") == CircuitBreakerState.OPEN


async def test_half_open_allows_exactly_one_call() -> None:
    import asyncio

    clock = _Clock()
    cb = _cb(clock=clock)
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    clock.advance(61.0)
    assert cb.state("cap") == CircuitBreakerState.HALF_OPEN

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow() -> str:
        started.set()
        await release.wait()
        return "ok"

    task = asyncio.create_task(cb.call("cap", slow()))
    await started.wait()
    # A second concurrent call while the test call is in-flight must be rejected.
    with pytest.raises(CircuitBreakerOpen):
        await cb.call("cap", _ok())
    release.set()
    assert await task == "ok"


async def test_reset_returns_to_closed() -> None:
    cb = _cb()
    for _ in range(3):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    assert cb.state("cap") == CircuitBreakerState.OPEN
    cb.reset("cap")
    assert cb.state("cap") == CircuitBreakerState.CLOSED


async def test_success_resets_failure_count_in_closed() -> None:
    cb = _cb()
    with pytest.raises(RuntimeError):
        await cb.call("cap", _fail())
    with pytest.raises(RuntimeError):
        await cb.call("cap", _fail())
    await cb.call("cap", _ok())  # resets count
    with pytest.raises(RuntimeError):
        await cb.call("cap", _fail())
    assert cb.state("cap") == CircuitBreakerState.CLOSED  # not tripped


async def test_trip_emits_event() -> None:
    store = EventStore()
    cb = CircuitBreaker(
        policy=CircuitBreakerPolicy(failure_threshold=2),
        event_store=store,
        event_bus=EventBus(),
        time_fn=_Clock(),
    )
    for _ in range(2):
        with pytest.raises(RuntimeError):
            await cb.call("cap", _fail())
    events = await store.read_stream("cap")
    assert any(e.type == "health.circuit_breaker_tripped" for e in events)


async def test_unknown_capability_defaults_closed() -> None:
    cb = _cb()
    assert cb.state("never-seen") == CircuitBreakerState.CLOSED
