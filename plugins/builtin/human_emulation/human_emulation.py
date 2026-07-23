"""plugins/builtin/human_emulation/human_emulation.py — Human Emulation Plugin.

Builtin plugin (ADR-013) bundling browser automation (Playwright) and human-like
input simulation (pyautogui) under the ``hermes.human`` capability namespace.
Tools are declared with ``@sdk.tool`` on methods and registered explicitly into
the kernel ``ToolRegistry`` via ``register_tools()`` after ``load()`` — no
global SDK state is required at import time.

AXIS CONTRACT: depends on kernel (domain, bus, registry, persistence) + plugins
(base, sdk). Never imported by kernel (loader is injected).
"""

from __future__ import annotations

import logging
from typing import Any

from kernel.bus import EventBus
from kernel.domain import HumanProfile, PluginManifest, Tool
from kernel.persistence import PersistenceRegistry
from kernel.registry import ToolRegistry
from plugins.base import BasePlugin
from plugins.sdk import sdk

from .browser_agent import BrowserAgent
from .input_simulator import InputSimulator

logger = logging.getLogger("hermes.human.plugin")


class HumanEmulationPlugin(BasePlugin):
    """Plugin for human-like browser automation and input simulation."""

    def __init__(self, manifest: PluginManifest) -> None:
        super().__init__(manifest)
        self._profiles: dict[str, HumanProfile] = {}
        self._agents: dict[str, BrowserAgent] = {}
        self._simulators: dict[str, InputSimulator] = {}
        self._persistence: PersistenceRegistry | None = None
        self._tool_registry: ToolRegistry | None = None

    def load(self) -> bool:
        logger.info("HumanEmulationPlugin loaded")
        return True

    def unload(self) -> bool:
        # Browser/pyautogui sessions are closed via the per-profile tools; the
        # plugin itself only drops its in-memory handles on unload (sync, no
        # event-loop assumptions — kernel.stop() owns async teardown).
        self._agents.clear()
        self._simulators.clear()
        self._profiles.clear()
        return True

    def get_capabilities(self) -> list[str]:
        return ["hermes.human.browser", "hermes.human.input"]

    # -- explicit tool registration (clean injection) --------------------- #
    def register_tools(self, tool_registry: ToolRegistry) -> None:
        """Register all human-emulation Tools into ``tool_registry`` (idempotent)."""
        self._tool_registry = tool_registry
        from plugins.sdk.tool import get_tools

        for meta in get_tools(HumanEmulationPlugin):
            tool = Tool(
                name=meta["name"],
                capability=meta["capability"],
                input_schema=meta["schema"] or {},
            )
            try:
                tool_registry.register_sync(tool)
            except ValueError:
                pass  # already registered

    # -- tools ------------------------------------------------------------- #
    @sdk.tool(
        name="browser_start",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {"profile_id": {"type": "string"}}},
    )
    async def browser_start(self, profile_id: str) -> dict[str, Any]:
        """Start a browser session for the given profile."""
        profile = self._profiles.get(profile_id)
        if profile is None:
            return {"error": f"Profile {profile_id} not found"}
        agent = BrowserAgent(profile, self._persistence)
        session = await agent.start()
        self._agents[profile_id] = agent
        return {"session_id": session.id, "status": session.status}

    @sdk.tool(
        name="browser_navigate",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {
            "profile_id": {"type": "string"},
            "url": {"type": "string"},
        }},
    )
    async def browser_navigate(self, profile_id: str, url: str) -> dict[str, Any]:
        """Navigate the profile's browser to ``url``."""
        agent = self._agents.get(profile_id)
        if agent is None:
            return {"error": "Browser not started"}
        await agent.navigate(url)
        return {"url": url, "status": "loaded"}

    @sdk.tool(
        name="browser_click",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {
            "profile_id": {"type": "string"},
            "selector": {"type": "string"},
        }},
    )
    async def browser_click(self, profile_id: str, selector: str) -> dict[str, Any]:
        """Click an element identified by ``selector``."""
        agent = self._agents.get(profile_id)
        if agent is None:
            return {"error": "Browser not started"}
        await agent.click(selector)
        return {"selector": selector, "action": "clicked"}

    @sdk.tool(
        name="browser_type",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {
            "profile_id": {"type": "string"},
            "selector": {"type": "string"},
            "text": {"type": "string"},
        }},
    )
    async def browser_type(
        self, profile_id: str, selector: str, text: str
    ) -> dict[str, Any]:
        """Type ``text`` into the element at ``selector`` (human speed)."""
        agent = self._agents.get(profile_id)
        if agent is None:
            return {"error": "Browser not started"}
        await agent.type_text(selector, text)
        return {"selector": selector, "text": text, "action": "typed"}

    @sdk.tool(
        name="browser_screenshot",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {"profile_id": {"type": "string"}}},
    )
    async def browser_screenshot(self, profile_id: str) -> dict[str, Any]:
        """Capture a screenshot of the profile's browser window."""
        agent = self._agents.get(profile_id)
        if agent is None:
            return {"error": "Browser not started"}
        path = await agent.screenshot()
        return {"screenshot_path": path}

    @sdk.tool(
        name="browser_close",
        capability="hermes.human.browser",
        schema={"type": "object", "properties": {"profile_id": {"type": "string"}}},
    )
    async def browser_close(self, profile_id: str) -> dict[str, Any]:
        """Close the profile's browser session."""
        agent = self._agents.pop(profile_id, None)
        if agent is None:
            return {"error": "Browser not started"}
        await agent.close()
        return {"status": "closed"}

    @sdk.tool(
        name="input_mouse_move",
        capability="hermes.human.input",
        schema={"type": "object", "properties": {
            "profile_id": {"type": "string"},
            "x": {"type": "integer"},
            "y": {"type": "integer"},
        }},
    )
    async def input_mouse_move(
        self, profile_id: str, x: int, y: int
    ) -> dict[str, Any]:
        """Move the mouse to (x, y) with a human-like curve."""
        sim = self._get_simulator(profile_id)
        if sim is None:
            return {"error": f"Profile {profile_id} not found"}
        await sim.mouse_move(x, y)
        return {"x": x, "y": y, "action": "moved"}

    @sdk.tool(
        name="input_type",
        capability="hermes.human.input",
        schema={"type": "object", "properties": {
            "profile_id": {"type": "string"},
            "text": {"type": "string"},
        }},
    )
    async def input_type(self, profile_id: str, text: str) -> dict[str, Any]:
        """Type ``text`` at the profile's human speed (rare typos)."""
        sim = self._get_simulator(profile_id)
        if sim is None:
            return {"error": f"Profile {profile_id} not found"}
        await sim.type_text(text)
        return {"text": text, "action": "typed"}

    # -- helpers ----------------------------------------------------------- #
    def register_profile(self, profile: HumanProfile) -> None:
        """Cache a profile in memory so tools can resolve ``profile_id``."""
        self._profiles[profile.id] = profile

    def _get_simulator(self, profile_id: str) -> InputSimulator | None:
        sim = self._simulators.get(profile_id)
        if sim is not None:
            return sim
        profile = self._profiles.get(profile_id)
        if profile is None:
            return None
        sim = InputSimulator(profile)
        self._simulators[profile_id] = sim
        return sim
