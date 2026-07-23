"""tests/test_capability_discovery.py — handler auto-discovery (ADR-018).

Verifies that ``CapabilityExecutor.autodiscover`` (via kernel.discovery) reflects
over already-loaded instances and wires their capabilities WITHOUT the kernel
importing any plugin module:

* A fake ``BaseAgent`` instance -> its capabilities become Task-routing handlers.
* A fake plugin instance with ``@sdk.tool`` methods -> those become handlers.

Both are then callable through ``executor.execute(capability, params)`` and
return a unified Artifact. No real plugin/agent imports occur.
"""

from __future__ import annotations

from typing import Any

import pytest
from kernel.agent import BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task
from plugins.sdk import sdk


# -- fake plugin with @sdk.tool handlers -------------------------------- #
class FakeDesktopPlugin:
    """Mimics DesktopControlPlugin's tool surface (no real pyautogui import)."""

    @sdk.tool(
        name="screenshot",
        capability="desktop.screenshot",
        schema={"type": "object", "properties": {}},
    )
    async def screenshot(self) -> dict[str, Any]:
        return {"image": "BASE64FAKE", "format": "png"}

    @sdk.tool(
        name="mouse_click",
        capability="desktop.click",
        schema={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    async def click(self, x: int, y: int = 0) -> dict[str, Any]:
        return {"ok": True, "x": x, "y": y}


# -- fake BaseAgent ----------------------------------------------------- #
class FakeEchoAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)
        self._running = False

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(
            type=task.capability or "text",
            content={"echo": task.name},
            format="json",
            source=f"agent:{self.name}",
            provenance=[f"task:{task.id}"],
        )

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {"state": "running" if self._running else "stopped"}


@pytest.mark.asyncio
async def test_autodiscover_wires_plugin_tool_methods() -> None:
    ex = CapabilityExecutor()
    plugin = FakeDesktopPlugin()
    n = ex.autodiscover([plugin])
    # two @sdk.tool methods registered
    assert n == 2
    assert "desktop.screenshot" in ex._handlers
    assert "desktop.click" in ex._handlers


@pytest.mark.asyncio
async def test_autodiscover_routes_plugin_execute() -> None:
    ex = CapabilityExecutor()
    ex.autodiscover([FakeDesktopPlugin()])
    art = await ex.execute("desktop.screenshot", {})
    assert isinstance(art, Artifact)
    assert art.type == "result"  # dict normalized (no "content"/"type" key)
    assert art.content == {"image": "BASE64FAKE", "format": "png"}
    assert "cap:desktop.screenshot" in art.provenance


@pytest.mark.asyncio
async def test_autodiscover_passes_params_to_plugin_method() -> None:
    ex = CapabilityExecutor()
    ex.autodiscover([FakeDesktopPlugin()])
    art = await ex.execute("desktop.click", {"x": 7, "y": 9})
    assert art.content == {"ok": True, "x": 7, "y": 9}


@pytest.mark.asyncio
async def test_autodiscover_wires_base_agent() -> None:
    ex = CapabilityExecutor()
    agent = FakeEchoAgent(Agent(name="echo", capabilities=["echo.ping"]))
    n = ex.autodiscover([agent])
    assert n == 1
    assert "echo.ping" in ex._handlers
    art = await ex.execute("echo.ping", {"name": "hi"})
    assert isinstance(art, Artifact)
    # Task.name is the capability (echo.ping); metadata carries the call params
    assert art.content == {"echo": "echo.ping"}


@pytest.mark.asyncio
async def test_autodiscover_mixed_instances() -> None:
    ex = CapabilityExecutor()
    plugin = FakeDesktopPlugin()
    agent = FakeEchoAgent(Agent(name="echo", capabilities=["echo.ping"]))
    n = ex.autodiscover([plugin, agent])
    assert n == 3
    # both surfaces callable
    assert isinstance(await ex.execute("desktop.screenshot", {}), Artifact)
    assert isinstance(await ex.execute("echo.ping", {"name": "x"}), Artifact)


@pytest.mark.asyncio
async def test_autodiscover_idempotent() -> None:
    ex = CapabilityExecutor()
    plugin = FakeDesktopPlugin()
    n1 = ex.autodiscover([plugin])
    n2 = ex.autodiscover([plugin])
    assert n1 == n2 == 2
    # no duplicate handler entries
    assert len([c for c in ex._handlers if c.startswith("desktop.")]) == 2
