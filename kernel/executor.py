"""kernel/executor.py — async Task execution engine for Hermes Kernel v2.

AXIS CONTRACT: imports only kernel.domain (Task/Event/Tool/Capability),
kernel.bus (EventBus), kernel.capability (CapabilityRegistry),
kernel.registry (ToolRegistry). No I/O — handlers are injected.

The Executor is the runtime heart: it drives a Task through its state machine
(PENDING -> QUEUED -> RUNNING -> COMPLETED|FAILED), emits lifecycle Events on
the shared EventBus (so the sync-barrier in bus.py can await completion), and
resolves a Task's tools/capability via the registries. This is the "Task
end-to-end through EventBus with Capability" path from FOCUS Phase 1 / P1.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from kernel.bus import EventBus
from kernel.capability import CapabilityRegistry
from kernel.domain import Capability, Event, Task, Tool
from kernel.registry import ToolRegistry

logger = logging.getLogger(__name__)

# Task lifecycle event types emitted on the bus
EVENT_TASK_QUEUED = "task.queued"
EVENT_TASK_STARTED = "task.started"
EVENT_TASK_COMPLETED = "task.completed"
EVENT_TASK_FAILED = "task.failed"

TaskHandler = Callable[[Task, dict[str, Any]], Awaitable[Any]]


class Executor:
    """Runs Tasks, emits lifecycle Events, resolves tools/capability.

    Handlers are registered by capability name. A Task is executed by looking
    up the handler for `task.capability`; the matching Capability (if any) and
    its bundled Tools are placed into the execution context for the handler.
    """

    def __init__(
        self,
        bus: EventBus,
        capability_registry: Optional[CapabilityRegistry] = None,
        tool_registry: Optional[ToolRegistry] = None,
    ) -> None:
        self._bus = bus
        self._caps = capability_registry
        self._tools = tool_registry
        self._handlers: dict[str, TaskHandler] = {}
        self._lock = asyncio.Lock()

    # --- handler registration --------------------------------------------- #
    def register_handler(self, capability: str, handler: TaskHandler) -> None:
        self._handlers[capability] = handler

    # --- capability / tool resolution ------------------------------------- #
    async def resolve_capability(self, task: Task) -> Optional[Capability]:
        """Resolve the Task's capability entity, if a registry is wired."""
        if not task.capability or self._caps is None:
            return None
        return await self._caps.get_by_name(task.capability)

    async def resolve_tools(self, task: Task) -> list[Tool]:
        """Resolve the Tools bundled by the Task's capability (FOCUS P1 path)."""
        if not task.capability or self._caps is None:
            return []
        return await self._caps.resolve_tools_by_name(task.capability)

    # --- execution ------------------------------------------------------- #
    async def submit(self, task: Task, ctx: Optional[dict[str, Any]] = None) -> Any:
        """Queue + run a Task through its state machine; return handler result."""
        ctx = dict(ctx or {})
        async with self._lock:
            task.status = "QUEUED"
        self._bus.publish(
            Event(type=EVENT_TASK_QUEUED, payload={"task_id": task.id}, source="executor")
        )
        return await self.run(task, ctx)

    async def run(self, task: Task, ctx: Optional[dict[str, Any]] = None) -> Any:
        """Execute a single Task, emitting lifecycle Events and resolving tools."""
        ctx = dict(ctx or {})
        if task.capability and task.capability not in self._handlers:
            raise KeyError(f"No handler registered for capability {task.capability!r}")

        async with self._lock:
            task.status = "RUNNING"
        self._bus.publish(
            Event(type=EVENT_TASK_STARTED, payload={"task_id": task.id}, source="executor")
        )

        # resolve capability + tools into the execution context
        ctx["capability"] = await self.resolve_capability(task)
        ctx["tools"] = await self.resolve_tools(task)

        try:
            if task.capability:
                handler = self._handlers[task.capability]
            else:
                handler = self._handlers.get("__default__")
                if handler is None:
                    raise KeyError("Task has no capability and no __default__ handler")
            result = await handler(task, ctx)
        except Exception as exc:  # fault containment: one task failure is isolated
            async with self._lock:
                task.status = "FAILED"
            self._bus.publish(
                Event(
                    type=EVENT_TASK_FAILED,
                    payload={"task_id": task.id, "error": repr(exc)},
                    source="executor",
                )
            )
            logger.exception("Task %s failed", task.id)
            raise

        async with self._lock:
            task.status = "COMPLETED"
        self._bus.publish(
            Event(
                type=EVENT_TASK_COMPLETED,
                payload={"task_id": task.id, "result": result},
                source="executor",
            )
        )
        return result
