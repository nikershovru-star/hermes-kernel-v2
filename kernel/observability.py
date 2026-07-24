"""kernel/observability.py — ObservabilityEngine (ADR-027).

Structured metrics, distributed-style tracing, correlation-id logging and a
health snapshot. All I/O is injectable so it can run with zero side effects.

AXIS CONTRACT: imports only ``kernel.observability_domain`` + ``kernel.events``.
No reverse dependency on workflow/agent — those *wire* this engine in (Stage 4/5).
"""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import datetime, timezone
from typing import Any

from kernel.events import (
    EventBus,
    EventStore,
    LogEntryEmitted,
    MetricRecorded,
    TraceSpanFinished,
    TraceSpanStarted,
)
from kernel.observability_domain import (
    LogEntry,
    MetricRecord,
    MetricType,
    TraceSpan,
)

logger = logging.getLogger("hermes.kernel.observability")

_LEVEL_RANK = {"debug": 0, "info": 1, "warn": 2, "error": 3}


class ObservabilityEngine:
    """Collect metrics, spans, logs; expose health + Prometheus export.

    Ring buffers cap in-memory retention (not a full LTS store). A persisted
    ``ObservabilityStore`` is optional; when absent, everything stays in RAM.
    """

    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        store: Any | None = None,
        clock: Any = None,
        rng: random.Random | None = None,
        sleep: Any = None,
        metrics_limit: int = 1000,
        logs_limit: int = 1000,
        spans_limit: int = 1000,
    ) -> None:
        self._bus = event_bus
        self._event_store = event_store
        self._store = store
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._rng = rng or random.Random()
        self._sleep = sleep or asyncio.sleep
        self._metrics_limit = metrics_limit
        self._logs_limit = logs_limit
        self._spans_limit = spans_limit
        # ring buffers (lists, trimmed from the front)
        self._metrics: list[MetricRecord] = []
        self._logs: list[LogEntry] = []
        self._spans: dict[str, TraceSpan] = {}  # span_id -> span
        self._started_at: datetime = self._clock()
        self._counters: dict[str, float] = {}
        self._last_error: str | None = None
        self._error_count: int = 0

    # -- metrics --------------------------------------------------------- #
    async def record_metric(
        self, name: str, value: float, labels: dict[str, str] | None = None, mtype: MetricType = MetricType.COUNTER
    ) -> MetricRecord:
        rec = MetricRecord(
            metric_id=uuid.uuid4().hex,
            name=name,
            value=value,
            type=mtype,
            labels=dict(labels or {}),
            timestamp=self._clock(),
        )
        self._metrics.append(rec)
        if len(self._metrics) > self._metrics_limit:
            self._metrics = self._metrics[-self._metrics_limit:]
        if mtype == MetricType.COUNTER:
            self._counters[name] = self._counters.get(name, 0.0) + value
        if self._store is not None:
            self._store.put_metric(rec)
        await self._emit(MetricRecorded(name, value, mtype.value, rec.labels, aggregate_id=labels.get("workflow_id", "") if labels else ""))
        return rec

    # -- tracing --------------------------------------------------------- #
    async def start_span(self, trace_id: str, span_name: str, parent_id: str | None = None, correlation_id: str | None = None) -> str:
        span_id = uuid.uuid4().hex
        span = TraceSpan(
            span_id=span_id,
            trace_id=trace_id,
            span_name=span_name,
            parent_id=parent_id,
            start_time=self._clock(),
            correlation_id=correlation_id,
        )
        self._spans[span_id] = span
        if len(self._spans) > self._spans_limit:
            # evict oldest by start_time
            oldest = min(self._spans.values(), key=lambda s: s.start_time)
            self._spans.pop(oldest.span_id, None)
        if self._store is not None:
            self._store.put_span(span)
        await self._emit(TraceSpanStarted(span_id, trace_id, span_name, parent_id, correlation_id))
        return span_id

    async def finish_span(self, span_id: str, status: str = "ok") -> TraceSpan | None:
        span = self._spans.get(span_id)
        if span is None:
            return None
        span.end_time = self._clock()
        span.status = status
        if status == "error":
            self._last_error = f"{span.trace_id}/{span.span_name}"
            self._error_count += 1
        if self._store is not None:
            self._store.put_span(span)
        await self._emit(TraceSpanFinished(span_id, span.trace_id, status, span.correlation_id))
        return span

    def get_trace(self, trace_id: str) -> list[TraceSpan]:
        spans = [s for s in self._spans.values() if s.trace_id == trace_id]
        return sorted(spans, key=lambda s: s.start_time)

    # -- logging --------------------------------------------------------- #
    async def log(self, level: str, message: str, correlation_id: str | None = None, context: dict[str, Any] | None = None) -> LogEntry:
        entry = LogEntry(
            log_id=uuid.uuid4().hex,
            level=level,
            message=message,
            correlation_id=correlation_id,
            context=dict(context or {}),
            timestamp=self._clock(),
        )
        self._logs.append(entry)
        if len(self._logs) > self._logs_limit:
            self._logs = self._logs[-self._logs_limit:]
        if level == "error":
            self._last_error = message
            self._error_count += 1
        if self._store is not None:
            self._store.put_log(entry)
        await self._emit(LogEntryEmitted(level, message, correlation_id, entry.context))
        return entry

    def get_logs(self, correlation_id: str | None = None, level_min: str = "debug") -> list[LogEntry]:
        min_rank = _LEVEL_RANK.get(level_min, 0)
        out = [
            e for e in self._logs
            if (correlation_id is None or e.correlation_id == correlation_id)
            and _LEVEL_RANK.get(e.level, 0) >= min_rank
        ]
        return out

    # -- health ---------------------------------------------------------- #
    def get_health_snapshot(self) -> dict[str, Any]:
        now = self._clock()
        uptime = (now - self._started_at).total_seconds()
        return {
            "uptime_seconds": uptime,
            "counters": dict(self._counters),
            "last_error": self._last_error,
            "error_count": self._error_count,
            "metrics_buffered": len(self._metrics),
            "logs_buffered": len(self._logs),
            "spans_active": len([s for s in self._spans.values() if s.end_time is None]),
        }

    # -- prometheus export ---------------------------------------------- #
    def export_metrics(self) -> str:
        """Return a basic Prometheus exposition-format text block.

        Counters are exported as cumulative; gauges/histograms as-is. No
        complex histogram bucket math (documented limitation).
        """
        lines: list[str] = []
        for name, total in self._counters.items():
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {total}")
        # also surface the most recent sample of every metric name seen
        seen: set[str] = set()
        for rec in reversed(self._metrics):
            if rec.name in seen:
                continue
            seen.add(rec.name)
            if rec.type == MetricType.COUNTER:
                continue  # already covered by cumulative counter
            suffix = "_bucket" if rec.type == MetricType.HISTOGRAM else ""
            lines.append(f"# TYPE {rec.name} {rec.type.value}")
            label_str = ""
            if rec.labels:
                label_str = "{" + ",".join(f'{k}="{v}"' for k, v in rec.labels.items()) + "}"
            lines.append(f"{rec.name}{suffix}{label_str} {rec.value}")
        return "\n".join(lines) + "\n"

    # -- helpers --------------------------------------------------------- #
    async def _emit(self, event: Any) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001
                pass
