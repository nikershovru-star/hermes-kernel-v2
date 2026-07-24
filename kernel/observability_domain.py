"""kernel/observability_domain.py — Observability domain models (ADR-027).

Isolated from ``kernel.domain`` by design (no clashing names there, but kept
self-contained so the axis stays ``kernel.observability_domain`` +
``kernel.events`` only, mirroring ``semantic_graph.py`` / ``marketplace_domain.py``
in ADR-025/026).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MetricType(str, Enum):
    COUNTER = "counter"
    HISTOGRAM = "histogram"
    GAUGE = "gauge"


class CorrelationId(str):
    """Opaque correlation id (workflow/agent/trace scoped)."""


class MetricRecord(BaseModel):
    metric_id: str
    name: str
    value: float
    type: MetricType
    labels: dict[str, str] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TraceSpan(BaseModel):
    span_id: str
    trace_id: str
    span_name: str
    parent_id: str | None = None
    start_time: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    end_time: datetime | None = None
    status: str = "ok"  # "ok" | "error"
    correlation_id: str | None = None


class LogEntry(BaseModel):
    log_id: str
    level: str  # "debug" | "info" | "warn" | "error"
    message: str
    correlation_id: str | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
