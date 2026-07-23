"""plugins/builtin/desktop_control/desktop_agent.py — event-driven DesktopAgent (ADR-017).

Converts desktop control from imperative plugin calls into an event-driven
``BaseAgent``: ``execute(task)`` routes the task capability through the kernel
``CommandBus`` → domain handler (pyautogui side effect) → ``DomainEvent``
published on the ``EventBus`` + appended to the ``EventStore`` → returns a
unified ``Artifact`` with a provenance chain of event ids.

The agent DOGFOODs ``CapabilityExecutor`` for its own capabilities and emits an
event for EVERY operation (no silent side effects). pyautogui/Pillow are lazy
optional deps; without them the agent raises a clear RuntimeError at call time.

AXIS CONTRACT: depends on kernel (agent, domain, events, capability) + plugins.sdk.
Never imports mcp/ or other builtins.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from typing import Any

from kernel.agent import BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task
from kernel.events import Command, CommandBus, DomainEvent, EventBus, EventStore

logger = logging.getLogger("hermes.desktop.agent")


def _require_pyautogui():
    try:
        import pyautogui  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "DesktopAgent requires pyautogui; install the 'desktop' extra:\n"
            "  pip install 'hermes-kernel-v2[desktop]'"
        ) from exc
    return pyautogui


def _require_pillow():
    try:
        from PIL import Image  # noqa: PLC0415  # type: ignore
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "DesktopAgent requires Pillow; install the 'desktop' extra"
        ) from exc
    return Image


# --------------------------------------------------------------------------- #
# Commands (intent -> domain logic emits events)
# --------------------------------------------------------------------------- #
class DesktopClick(Command):
    def __init__(self, agent_id: str, x: int, y: int, button: str = "left") -> None:
        super().__init__(aggregate_id=agent_id)
        self.x = x
        self.y = y
        self.button = button


class DesktopType(Command):
    def __init__(self, agent_id: str, text: str) -> None:
        super().__init__(aggregate_id=agent_id)
        self.text = text


class DesktopScreenshot(Command):
    def __init__(self, agent_id: str, region: list[int] | None = None) -> None:
        super().__init__(aggregate_id=agent_id)
        self.region = region


class DesktopAgent(BaseAgent):
    """Event-driven desktop automation agent (ADR-017)."""

    def __init__(
        self,
        agent_entity: Agent,
        bus: EventBus,
        store: EventStore,
        command_bus: CommandBus,
        vision: Any = None,
    ) -> None:
        super().__init__(agent_entity)
        self._bus = bus
        self._store = store
        self._command_bus = command_bus
        self._vision = vision
        self._capability_executor = CapabilityExecutor()
        self._running = False
        self._event_ids: list[str] = []
        self._register_commands()

    # -- command wiring (domain handlers emit events) --------------------- #
    def _register_commands(self) -> None:
        self._command_bus.register(DesktopClick, self._handle_click)
        self._command_bus.register(DesktopType, self._handle_type)
        self._command_bus.register(DesktopScreenshot, self._handle_screenshot)

    async def _handle_click(self, cmd: DesktopClick) -> None:
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.click, x=cmd.x, y=cmd.y, button=cmd.button)
        await self._emit(
            DomainEvent(
                type="desktop.clicked",
                aggregate_id=cmd.aggregate_id,
                payload={"x": cmd.x, "y": cmd.y, "button": cmd.button},
            )
        )

    async def _handle_type(self, cmd: DesktopType) -> None:
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.write, cmd.text)
        await self._emit(
            DomainEvent(
                type="desktop.typed",
                aggregate_id=cmd.aggregate_id,
                payload={"text": cmd.text},
            )
        )

    async def _handle_screenshot(self, cmd: DesktopScreenshot) -> None:
        pyautogui = _require_pyautogui()
        img = await asyncio.to_thread(pyautogui.screenshot, region=tuple(cmd.region) if cmd.region else None)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
        await self._emit(
            DomainEvent(
                type="desktop.screenshot_taken",
                aggregate_id=cmd.aggregate_id,
                payload={"format": "png", "encoding": "base64", "image": encoded},
            )
        )

    async def _emit(self, event: DomainEvent) -> None:
        await self._store.append(event)
        self._bus.publish(event)
        self._event_ids.append(event.id)

    # -- BaseAgent lifecycle ---------------------------------------------- #
    async def start(self) -> str:
        self._running = True
        await self._emit(
            DomainEvent(
                type="agent.started",
                aggregate_id=self.agent_id,
                payload={"agent_type": "desktop"},
            )
        )
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        if not self._running:
            return False
        self._running = False
        await self._emit(
            DomainEvent(
                type="agent.stopped",
                aggregate_id=agent_id,
                payload={"reason": "explicit_stop"},
            )
        )
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        if not self._running:
            raise RuntimeError(f"agent {agent_id} is not running")
        cap = task.capability or ""
        # route via CommandBus -> handler -> event emission
        if cap == "desktop.click":
            x, y = task.metadata.get("x", 0), task.metadata.get("y", 0)
            await self._command_bus.send(DesktopClick(agent_id, x, y))
        elif cap == "desktop.type":
            await self._command_bus.send(DesktopType(agent_id, task.metadata.get("text", "")))
        elif cap == "desktop.screenshot":
            region = task.metadata.get("region")
            await self._command_bus.send(DesktopScreenshot(agent_id, region))
            last = self._last_event_of_type("desktop.screenshot_taken")
            return Artifact(
                type="screenshot",
                content=last.payload["image"] if last else None,
                format="png",
                source="agent:desktop",
                provenance=self._event_ids[-1:] if self._event_ids else [],
            )
        else:
            raise ValueError(f"unsupported desktop capability: {cap}")
        return Artifact(
            type=cap,
            content={"ok": True},
            format="json",
            source="agent:desktop",
            provenance=list(self._event_ids),
        )

    def _last_event_of_type(self, etype: str) -> DomainEvent | None:
        for e in reversed(self._store._events):
            if e.type == etype and e.aggregate_id == self.agent_id:
                return e
        return None

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "name": self.name,
            "state": "running" if self._running else "stopped",
            "capabilities": self.capabilities,
            "events_emitted": len(self._event_ids),
        }
