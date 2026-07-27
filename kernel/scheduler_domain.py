"""kernel/scheduler_domain.py — Workflow Scheduler domain models (ADR-032).

Isolated from the rest of the kernel (mirrors ``config_domain`` /
``resilience_domain``): imports only ``kernel.domain`` (for ``WorkflowTrigger``
reuse) + ``kernel.events`` via the event classes below. Self-contained axis
leaf.

Field semantics (see ADR-032 for honest limitations):
  * ``ScheduleType`` — CRON | DELAY | INTERVAL | ONCE.
  * ``WorkflowSchedule`` — a persisted schedule row. ``expression`` is a 5-field
    cron string for CRON, an ISO-8601 delay/offset for DELAY, or a seconds
    count for INTERVAL.
  * ``TriggerContext`` — carried on each ``WorkflowTriggered`` emission.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _new_id() -> str:
    """uuid4 hex string — same convention as kernel/domain.py."""
    return str(uuid.uuid4())


class ScheduleType(str, Enum):
    """Kind of schedule expression."""

    CRON = "cron"
    DELAY = "delay"
    INTERVAL = "interval"
    ONCE = "once"


class WorkflowSchedule(BaseModel):
    """A persisted workflow schedule (ADR-032)."""

    schedule_id: str = Field(default_factory=_new_id)
    workflow_id: str
    type: ScheduleType
    expression: str
    enabled: bool = True
    last_run: datetime | None = None
    next_run: datetime | None = None
    run_count: int = 0
    max_runs: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)
    deleted: bool = False

    def is_due(self, now: datetime) -> bool:
        """True when enabled, not deleted, and next_run is at/before ``now``."""
        return self.enabled and not self.deleted and self.next_run is not None and self.next_run <= now


class TriggerContext(BaseModel):
    """Context attached to a triggered run (ADR-032)."""

    triggered_at: datetime
    scheduled_by: str = "scheduler"
    delay_ms: int | None = None
