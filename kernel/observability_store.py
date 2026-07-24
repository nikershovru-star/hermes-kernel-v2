"""kernel/observability_store.py — observability persistence (ADR-027).

In-memory CRUD + optional SQLite, mirroring ``PlanStore`` / ``GraphStore`` /
``MarketplaceStore``. Tables: ``metrics``, ``spans``, ``logs``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from kernel.observability_domain import LogEntry, MetricRecord, TraceSpan


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ObservabilityStore:
    """Persist metric records, trace spans and log entries."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._mem_metrics: list[MetricRecord] = []
        self._mem_spans: dict[str, TraceSpan] = {}
        self._mem_logs: list[LogEntry] = []
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._load_all()

    # -- schema ---------------------------------------------------------- #
    def _init_db(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS metrics (metric_id TEXT PRIMARY KEY, name TEXT, data TEXT, ts TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS spans (span_id TEXT PRIMARY KEY, trace_id TEXT, data TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS logs (log_id TEXT PRIMARY KEY, correlation_id TEXT, level TEXT, data TEXT, ts TEXT)"
        )
        self._conn.commit()

    def _load_all(self) -> None:
        assert self._conn is not None
        for row in self._conn.execute("SELECT metric_id, name, data, ts FROM metrics"):
            self._mem_metrics.append(MetricRecord.model_validate_json(row[2]))
        for row in self._conn.execute("SELECT span_id, trace_id, data FROM spans"):
            self._mem_spans[row[0]] = TraceSpan.model_validate_json(row[2])
        for row in self._conn.execute("SELECT log_id, correlation_id, level, data, ts FROM logs"):
            self._mem_logs.append(LogEntry.model_validate_json(row[3]))

    # -- metrics --------------------------------------------------------- #
    def put_metric(self, rec: MetricRecord) -> None:
        self._mem_metrics.append(rec)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO metrics (metric_id, name, data, ts) VALUES (?, ?, ?, ?)",
                (rec.metric_id, rec.name, rec.model_dump_json(), rec.timestamp.isoformat()),
            )
            self._conn.commit()

    def query_metrics(self, name: str | None = None, since: datetime | None = None, until: datetime | None = None) -> list[MetricRecord]:
        out = self._mem_metrics
        if name is not None:
            out = [m for m in out if m.name == name]
        if since is not None:
            out = [m for m in out if m.timestamp >= since]
        if until is not None:
            out = [m for m in out if m.timestamp <= until]
        return out

    # -- spans ----------------------------------------------------------- #
    def put_span(self, span: TraceSpan) -> None:
        self._mem_spans[span.span_id] = span
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO spans (span_id, trace_id, data) VALUES (?, ?, ?)",
                (span.span_id, span.trace_id, span.model_dump_json()),
            )
            self._conn.commit()

    def get_trace(self, trace_id: str) -> list[TraceSpan]:
        return [s for s in self._mem_spans.values() if s.trace_id == trace_id]

    # -- logs ------------------------------------------------------------ #
    def put_log(self, entry: LogEntry) -> None:
        self._mem_logs.append(entry)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO logs (log_id, correlation_id, level, data, ts) VALUES (?, ?, ?, ?, ?)",
                (entry.log_id, entry.correlation_id or "", entry.level, entry.model_dump_json(), entry.timestamp.isoformat()),
            )
            self._conn.commit()

    def query_logs(self, correlation_id: str | None = None, level_min: str = "debug") -> list[LogEntry]:
        _rank = {"debug": 0, "info": 1, "warn": 2, "error": 3}
        min_rank = _rank.get(level_min, 0)
        return [
            e for e in self._mem_logs
            if (correlation_id is None or e.correlation_id == correlation_id)
            and _rank.get(e.level, 0) >= min_rank
        ]
