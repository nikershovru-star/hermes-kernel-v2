"""plugins/builtin/desktop_control/events.py — desktop-specific DomainEvents (ADR-017).

Thin, typed subclasses of ``kernel.events.DomainEvent`` so callers/emitted events
read semantically (``DesktopScreenshotTaken(...)``) instead of raw
``DomainEvent(type="desktop.screenshot_taken", ...)``. All events remain valid
``DomainEvent``s and flow through the kernel ``EventBus`` unchanged.

AXIS CONTRACT: depends on kernel.events + kernel.domain only.
"""

from __future__ import annotations

from kernel.domain import Artifact
from kernel.events import DomainEvent


class DesktopScreenshotTaken(DomainEvent):
    """A desktop screenshot was captured."""

    def __init__(
        self, aggregate_id: str, screenshot_artifact: Artifact | None = None
    ) -> None:
        payload: dict = {}
        if screenshot_artifact is not None:
            payload = {
                "artifact_id": screenshot_artifact.id,
                "format": screenshot_artifact.format,
            }
        super().__init__(
            type="desktop.screenshot_taken",
            aggregate_id=aggregate_id,
            payload=payload,
        )


class DesktopClicked(DomainEvent):
    """A mouse click happened at (x, y)."""

    def __init__(self, aggregate_id: str, x: int, y: int, element_label: str | None = None) -> None:
        super().__init__(
            type="desktop.clicked",
            aggregate_id=aggregate_id,
            payload={"x": x, "y": y, "element_label": element_label},
        )


class DesktopTyped(DomainEvent):
    """Text was typed on the desktop."""

    def __init__(self, aggregate_id: str, text: str, element_label: str | None = None) -> None:
        super().__init__(
            type="desktop.typed",
            aggregate_id=aggregate_id,
            payload={"text": text, "element_label": element_label},
        )


class AgentStarted(DomainEvent):
    def __init__(self, agent_id: str, agent_type: str) -> None:
        super().__init__(
            type="agent.started",
            aggregate_id=agent_id,
            payload={"agent_type": agent_type},
        )


class AgentStopped(DomainEvent):
    def __init__(self, agent_id: str, reason: str) -> None:
        super().__init__(
            type="agent.stopped",
            aggregate_id=agent_id,
            payload={"reason": reason},
        )


class CapabilityExecuted(DomainEvent):
    def __init__(self, capability: str, artifact_id: str, duration_ms: float) -> None:
        super().__init__(
            type="capability.executed",
            aggregate_id=capability,
            payload={"artifact_id": artifact_id, "duration_ms": duration_ms},
        )


class TaskAssigned(DomainEvent):
    def __init__(self, task_id: str, agent_id: str, capability: str) -> None:
        super().__init__(
            type="task.assigned",
            aggregate_id=task_id,
            payload={"agent_id": agent_id, "capability": capability},
        )
