"""tests/test_observability.py — ObservabilityEngine (ADR-027).

Deterministic: injectable rng, clock, sleep; no real I/O.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

import pytest
from kernel.events import EventBus, EventStore
from kernel.observability import ObservabilityEngine
from kernel.observability_domain import MetricRecord, MetricType, TraceSpan


def _eng(**kw):
    return ObservabilityEngine(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(7), **kw)


async def test_record_counter_metric() -> None:
    eng = _eng()
    await eng.record_metric("wf.exec", 1.0, {"workflow_id": "w1"}, MetricType.COUNTER)
    snap = eng.get_health_snapshot()
    assert snap["counters"]["wf.exec"] == 1.0


async def test_record_histogram_metric_buffered() -> None:
    eng = _eng()
    await eng.record_metric("latency", 12.0, {}, MetricType.HISTOGRAM)
    assert len(eng._metrics) == 1
    assert eng._metrics[0].type == MetricType.HISTOGRAM


async def test_span_lifecycle() -> None:
    eng = _eng()
    sid = await eng.start_span("t1", "execute")
    span = await eng.finish_span(sid, "ok")
    assert span is not None and span.end_time is not None and span.status == "ok"
    assert len(eng.get_trace("t1")) == 1


async def test_span_finish_unknown_returns_none() -> None:
    eng = _eng()
    assert await eng.finish_span("missing", "ok") is None


async def test_log_buffering_with_correlation() -> None:
    eng = _eng()
    await eng.log("info", "hello", correlation_id="c1")
    await eng.log("error", "boom", correlation_id="c2")
    assert len(eng.get_logs()) == 2
    assert len(eng.get_logs("c1")) == 1


async def test_log_level_filter() -> None:
    eng = _eng()
    await eng.log("debug", "d")
    await eng.log("info", "i")
    await eng.log("error", "e")
    assert len(eng.get_logs(level_min="error")) == 1
    assert len(eng.get_logs(level_min="info")) == 2


async def test_health_snapshot_uptime_and_error() -> None:
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    later = now + timedelta(seconds=42)
    eng = _eng(clock=lambda: now)
    eng._started_at = now
    eng._clock = lambda: later  # type: ignore[assignment]
    await eng.log("error", "fail")
    snap = eng.get_health_snapshot()
    assert int(snap["uptime_seconds"]) == 42
    assert snap["last_error"] == "fail"
    assert snap["error_count"] == 1


async def test_ring_buffer_eviction_metrics() -> None:
    eng = ObservabilityEngine(rng=random.Random(1), metrics_limit=3)
    for i in range(5):
        await eng.record_metric(f"m{i}", float(i))
    assert len(eng._metrics) == 3
    assert eng._metrics[0].name == "m2"


async def test_span_ring_eviction_oldest() -> None:
    eng = ObservabilityEngine(rng=random.Random(1), spans_limit=2)
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    s1 = await eng.start_span("t1", "a")
    eng._spans[s1].start_time = t0  # oldest
    s2 = await eng.start_span("t2", "b")
    s3 = await eng.start_span("t3", "c")
    assert s1 not in eng._spans  # evicted
    assert set(eng._spans.keys()) == {s2, s3}


async def test_metric_recorded_event_emitted() -> None:
    store = EventStore()
    eng = ObservabilityEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    await eng.record_metric("x", 1.0)
    assert any(e.type == "obs.metric_recorded" for e in store._events)


async def test_span_events_emitted() -> None:
    store = EventStore()
    eng = ObservabilityEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    sid = await eng.start_span("t1", "run")
    await eng.finish_span(sid, "ok")
    types = {e.type for e in store._events}
    assert "obs.span_started" in types and "obs.span_finished" in types


async def test_export_metrics_prometheus_format() -> None:
    eng = _eng()
    await eng.record_metric("wf.exec", 3.0, {}, MetricType.COUNTER)
    await eng.record_metric("latency", 9.0, {}, MetricType.GAUGE)
    out = eng.export_metrics()
    assert "wf.exec 3.0" in out
    assert "latency 9.0" in out
    assert out.startswith("# TYPE")
