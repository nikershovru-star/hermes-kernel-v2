"""tests/test_resilience.py — ResilienceEngine unit tests (ADR-031).

Deterministic: injectable clock + a no-op sleep (backoff never really waits).
asyncio_mode = auto (no decorators).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kernel.events import EventStore
from kernel.resilience import ResilienceEngine
from kernel.resilience_domain import (
    CircuitBreakerOpenError,
    CircuitState,
    ResilienceCircuitConfig,
    ResilienceRetryPolicy,
    RetryExhaustedError,
)
from kernel.resilience_store import ResilienceStore


class _Clock:
    def __init__(self, start: datetime | None = None):
        self._t = start or datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._t

    def advance_ms(self, ms: float) -> None:
        self._t = self._t + timedelta(milliseconds=ms)


async def _noop_sleep(_seconds: float) -> None:
    return None


def _engine(clock=None, store=None, event_store=None, bus=None, metrics=None):
    return ResilienceEngine(
        store=store,
        event_bus=bus,
        event_store=event_store,
        clock=clock or _Clock(),
        sleep=_noop_sleep,
        metrics=metrics,
    )


# 1 — circuit CLOSED → OPEN after failure_threshold
async def test_circuit_opens_after_threshold():
    eng = _engine()
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=3))
    assert eng.get_circuit_status("c") is CircuitState.CLOSED
    for _ in range(3):
        with pytest.raises(ConnectionError):
            async with eng.call_with_circuit("c"):
                raise ConnectionError("boom")
    assert eng.get_circuit_status("c") is CircuitState.OPEN


# 2 — OPEN rejects immediately with CircuitBreakerOpenError
async def test_open_circuit_rejects_immediately():
    eng = _engine()
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    assert eng.get_circuit_status("c") is CircuitState.OPEN
    executed = False
    with pytest.raises(CircuitBreakerOpenError):
        async with eng.call_with_circuit("c"):
            executed = True  # pragma: no cover - must not run
    assert executed is False


# 3 — OPEN → HALF_OPEN after recovery timeout
async def test_open_to_half_open_after_timeout():
    clk = _Clock()
    eng = _engine(clock=clk)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1, recovery_timeout_ms=1000))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    assert eng.get_circuit_status("c") is CircuitState.OPEN
    clk.advance_ms(1001)
    assert eng.get_circuit_status("c") is CircuitState.HALF_OPEN


# 4 — HALF_OPEN success → CLOSED
async def test_half_open_success_closes():
    clk = _Clock()
    eng = _engine(clock=clk)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1, recovery_timeout_ms=500))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    clk.advance_ms(600)
    assert eng.get_circuit_status("c") is CircuitState.HALF_OPEN
    async with eng.call_with_circuit("c"):
        pass  # success
    assert eng.get_circuit_status("c") is CircuitState.CLOSED


# 5 — HALF_OPEN failure → OPEN again
async def test_half_open_failure_reopens():
    clk = _Clock()
    eng = _engine(clock=clk)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1, recovery_timeout_ms=500))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    clk.advance_ms(600)
    assert eng.get_circuit_status("c") is CircuitState.HALF_OPEN
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("still bad")
    assert eng.get_circuit_status("c") is CircuitState.OPEN


# 6 — retry succeeds on 2nd attempt
async def test_retry_success_on_second_attempt():
    eng = _engine()
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("transient")
        return "ok"

    result = await eng.retry(flaky, ResilienceRetryPolicy(max_attempts=3), task_id="t1")
    assert result == "ok"
    assert calls["n"] == 2


# 7 — retry exhausted emits RetryExhausted + raises
async def test_retry_exhausted_emits_event():
    store = EventStore()
    eng = _engine(event_store=store)

    async def always_fail():
        raise ValueError("nope")

    with pytest.raises(RetryExhaustedError):
        await eng.retry(always_fail, ResilienceRetryPolicy(max_attempts=3), task_id="t2")
    events = await store.read_stream("t2")
    assert any(e.type == "res.retry_exhausted" for e in events)
    assert sum(1 for e in events if e.type == "res.retry_attempted") == 2  # attempts 1,2 retried


# 8 — backoff deterministic exponential (no jitter)
async def test_backoff_deterministic():
    pol = ResilienceRetryPolicy(backoff_base_ms=100, max_backoff_ms=1000)
    assert [pol.backoff_for(a) for a in range(1, 6)] == [100, 200, 400, 800, 1000]
    # slept values are recorded through the injected sleep
    slept: list[float] = []

    async def rec_sleep(seconds: float) -> None:
        slept.append(seconds)

    eng = ResilienceEngine(sleep=rec_sleep, clock=_Clock())
    calls = {"n": 0}

    async def flaky():
        calls["n"] += 1
        raise ConnectionError("x")

    with pytest.raises(RetryExhaustedError):
        await eng.retry(flaky, ResilienceRetryPolicy(max_attempts=3, backoff_base_ms=100), task_id="t3")
    assert slept == [0.1, 0.2]  # backoff after attempts 1 and 2


# 9 — non-retryable exception stops immediately
async def test_non_retryable_stops_immediately():
    eng = _engine()
    calls = {"n": 0}

    async def boom():
        calls["n"] += 1
        raise KeyError("k")

    with pytest.raises(RetryExhaustedError):
        await eng.retry(
            boom,
            ResilienceRetryPolicy(max_attempts=5, retryable_exceptions=["ConnectionError"]),
            task_id="t4",
        )
    assert calls["n"] == 1  # never retried


# 10 — DLQ enqueue + list_dead_letter
async def test_dlq_enqueue_and_list():
    store = EventStore()
    eng = _engine(event_store=store)
    entry = await eng.enqueue_dead_letter({"task": "foo"}, "failed", 3)
    assert entry.status == "pending"
    pending = eng.list_dead_letter("pending")
    assert len(pending) == 1 and pending[0].original_task == {"task": "foo"}
    events = await store.read_stream(entry.entry_id)
    assert any(e.type == "res.dead_letter_enqueued" for e in events)


# 11 — DLQ replay success marks replayed; failure keeps pending
async def test_dlq_replay_success_and_failure():
    eng = _engine()
    e1 = await eng.enqueue_dead_letter({"n": 1}, "err", 2)

    async def replay_ok(task):
        return "done:" + str(task["n"])

    result = await eng.replay_dead_letter(e1.entry_id, replay_ok)
    assert result == "done:1"
    assert eng.list_dead_letter("pending") == []
    assert len(eng.list_dead_letter("replayed")) == 1

    e2 = await eng.enqueue_dead_letter({"n": 2}, "err", 2)

    async def replay_fail(task):
        raise ConnectionError("still down")

    with pytest.raises(ConnectionError):
        await eng.replay_dead_letter(e2.entry_id, replay_fail)
    # stays pending, attempts bumped
    pending = eng.list_dead_letter("pending")
    assert len(pending) == 1 and pending[0].entry_id == e2.entry_id
    assert pending[0].attempts == 3


# 12 — circuit status query + unknown circuit raises; auto-register on call
async def test_circuit_status_query_and_autoregister():
    eng = _engine()
    with pytest.raises(KeyError):
        eng.get_circuit_status("missing")
    # call_with_circuit auto-registers an unknown circuit as CLOSED
    async with eng.call_with_circuit("auto"):
        pass
    assert eng.get_circuit_status("auto") is CircuitState.CLOSED


# bonus 13 — metrics counter increments on circuit open
async def test_metrics_circuit_opened_counter():
    class _Metrics:
        def __init__(self):
            self.records = []

        async def record_metric(self, name, value, labels=None):
            self.records.append((name, value, labels))

    m = _Metrics()
    eng = _engine(metrics=m)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    assert any(r[0] == "res.circuit_opened" for r in m.records)


# bonus 14 — discard_dead_letter removes from pending
async def test_discard_dead_letter():
    eng = _engine()
    e = await eng.enqueue_dead_letter({"x": 1}, "err", 1)
    assert eng.discard_dead_letter(e.entry_id) is True
    assert eng.list_dead_letter("pending") == []
    assert len(eng.list_dead_letter("discarded")) == 1
    assert eng.discard_dead_letter("nonexistent") is False


# bonus 15 — engine persists circuit + retry to store
async def test_engine_persists_to_store():
    st = ResilienceStore()
    eng = _engine(store=st)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    row = st.get_circuit("c")
    assert row is not None and row["state"] == "open"

    async def flaky():
        raise ConnectionError("x")

    with pytest.raises(RetryExhaustedError):
        await eng.retry(flaky, ResilienceRetryPolicy(max_attempts=2), task_id="tt")
    assert len(st.list_retries("tt")) == 1  # one retry journalled before exhaustion


# bonus 16 — register_circuit replaces config and keeps name authoritative
async def test_register_circuit_replaces_and_fixes_name():
    eng = _engine()
    eng.register_circuit("real", ResilienceCircuitConfig(name="wrong", failure_threshold=5))
    rt = eng._circuits["real"]
    assert rt.config.name == "real" and rt.config.failure_threshold == 5
    # replace updates config
    eng.register_circuit("real", ResilienceCircuitConfig(name="real", failure_threshold=2))
    assert eng._circuits["real"].config.failure_threshold == 2


# bonus 17 — half_open allows up to half_open_max_calls trial calls
async def test_half_open_max_calls_enforced():
    clk = _Clock()
    eng = _engine(clock=clk)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1, recovery_timeout_ms=500, half_open_max_calls=1))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    clk.advance_ms(600)  # → HALF_OPEN
    assert eng.get_circuit_status("c") is CircuitState.HALF_OPEN
    # first half-open trial succeeds → CLOSED
    async with eng.call_with_circuit("c"):
        pass
    assert eng.get_circuit_status("c") is CircuitState.CLOSED
    # a fresh half-open cycle (trip, recover) then exceed half_open_max_calls
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    clk.advance_ms(600)
    assert eng.get_circuit_status("c") is CircuitState.HALF_OPEN
    # consuming the single allowed half-open call as a failure re-opens
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("bad")
    assert eng.get_circuit_status("c") is CircuitState.OPEN
    # now further calls rejected while OPEN
    with pytest.raises(CircuitBreakerOpenError):
        async with eng.call_with_circuit("c"):
            pass
