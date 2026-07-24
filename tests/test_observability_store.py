"""tests/test_observability_store.py — ObservabilityStore persistence (ADR-027)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kernel.observability_domain import LogEntry, MetricRecord, MetricType, TraceSpan
from kernel.observability_store import ObservabilityStore


def _m(name, value=1.0, mtype=MetricType.COUNTER):
    return MetricRecord(metric_id=name, name=name, value=value, type=mtype)


def _span(trace_id="t1", span_name="s"):
    return TraceSpan(span_id=trace_id + span_name, trace_id=trace_id, span_name=span_name)


def _log(level="info", msg="m", corr=None):
    return LogEntry(log_id=msg, level=level, message=msg, correlation_id=corr)


def test_put_query_metric_memory() -> None:
    s = ObservabilityStore()
    s.put_metric(_m("wf.exec"))
    assert len(s.query_metrics("wf.exec")) == 1


def test_metric_query_since_until() -> None:
    s = ObservabilityStore()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t1 = t0 + timedelta(seconds=10)
    t2 = t0 + timedelta(seconds=20)
    m0 = _m("a"); m0.timestamp = t0
    m1 = _m("a"); m1.timestamp = t1
    m2 = _m("a"); m2.timestamp = t2
    s.put_metric(m0); s.put_metric(m1); s.put_metric(m2)
    mid = s.query_metrics("a", since=t1, until=t2)
    assert len(mid) == 2


def test_sqlite_metric_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "obs.db")
    s = ObservabilityStore(db)
    s.put_metric(_m("wf.exec", 2.0))
    s2 = ObservabilityStore(db)
    assert s2.query_metrics("wf.exec")[0].value == 2.0


def test_sqlite_span_roundtrip_and_get_trace(tmp_path) -> None:
    db = str(tmp_path / "obs.db")
    s = ObservabilityStore(db)
    s.put_span(_span("t1", "a"))
    s.put_span(_span("t1", "b"))
    s.put_span(_span("t2", "c"))
    s2 = ObservabilityStore(db)
    trace = s2.get_trace("t1")
    assert len(trace) == 2


def test_sqlite_log_roundtrip_and_query(tmp_path) -> None:
    db = str(tmp_path / "obs.db")
    s = ObservabilityStore(db)
    s.put_log(_log("info", "hi", "c1"))
    s.put_log(_log("error", "boom", "c1"))
    s2 = ObservabilityStore(db)
    assert len(s2.query_logs("c1")) == 2
    assert len(s2.query_logs("c1", level_min="error")) == 1


def test_log_query_no_correlation() -> None:
    s = ObservabilityStore()
    s.put_log(_log("info", "x", "c1"))
    s.put_log(_log("info", "y", None))
    assert len(s.query_logs()) == 2
    assert len(s.query_logs("c1")) == 1


def test_get_trace_empty_when_absent() -> None:
    s = ObservabilityStore()
    assert s.get_trace("nope") == []


def test_in_memory_fallback_no_db() -> None:
    s = ObservabilityStore()
    s.put_span(_span("t1", "a"))
    s.put_log(_log("info", "m", "c1"))
    assert len(s.get_trace("t1")) == 1
    assert len(s.query_logs("c1")) == 1
