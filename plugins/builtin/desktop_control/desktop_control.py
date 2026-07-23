"""plugins/builtin/desktop_control/desktop_control.py — desktop automation plugin.

Provides mouse / keyboard / screenshot control as MCP-exposed Tools under the
``hermes.desktop`` capability. Builtin plugin (ships with the kernel); optional
deps ``pyautogui`` + ``Pillow`` are imported lazily inside the tool calls so the
core install never requires a GUI stack.

AXIS CONTRACT: depends on kernel (domain, registry) + plugins.sdk. Never imports
mcp/, and never triggers a kernel->plugins inversion. ``tach check`` stays green.

Design notes
------------
- ``DesktopControlPlugin`` is a ``BasePlugin`` whose ``@sdk.tool``-decorated
  methods declare the tool surface. Tools are registered into the kernel
  ``ToolRegistry`` explicitly via ``register_tools(tool_registry)`` (and the
  Agent via ``register_agent(agent_registry)``) — called by the kernel after
  ``load()``. This keeps the plugin import-side-effect free (no global SDK
  state required at import time) while still using the SDK metadata decorators.
- Every tool is ``async`` and offloads the blocking ``pyautogui`` call to a
  worker thread via ``asyncio.to_thread`` so the kernel event loop is never
  blocked by a real mouse move.
- Desktop control is a host-global singleton; tool effects are not workspace
  scoped (the plugin is still registered with a manifest like any plugin).
"""

from __future__ import annotations

import asyncio
import base64
import io
import platform
from typing import Any

from kernel.domain import PluginManifest, Tool  # type: ignore[import-not-found]
from plugins.base import BasePlugin
from plugins.sdk import sdk


def _require_pyautogui():
    """Lazily import pyautogui (optional dep); raise a clear error if missing."""
    try:
        import pyautogui  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError(
            "pyautogui is not installed; install the 'desktop' extra:\n"
            "  pip install 'hermes-kernel-v2[desktop]'"
        ) from exc
    return pyautogui


def _require_pillow():
    """Lazily import Pillow (optional dep); raise a clear error if missing."""
    try:
        from PIL import Image  # noqa: PLC0415  # type: ignore
    except ImportError as exc:  # pragma: no cover - depends on host install
        raise RuntimeError(
            "Pillow is not installed; install the 'desktop' extra:\n"
            "  pip install 'hermes-kernel-v2[desktop]'"
        ) from exc
    return Image


class DesktopControlPlugin(BasePlugin):
    """Mouse / keyboard / screenshot control exposed as kernel Tools."""

    def __init__(self, manifest: PluginManifest) -> None:
        super().__init__(manifest)
        # tool registry is injected at load() time (lazy; may be None if the
        # SDK was not configured before the plugin was constructed).
        self._tool_registry: Any = None
        self._loaded = False

    # -- plugin lifecycle -------------------------------------------------- #
    def load(self) -> bool:
        """Initialise the plugin: validate platform, import optional deps.

        Returns True if the plugin can operate on this host. Raises RuntimeError
        on an unsupported platform so the loader logs + skips it cleanly.
        """
        system = platform.system()
        if system not in ("Windows", "Linux", "Darwin"):
            raise RuntimeError(
                f"desktop_control unsupported on platform {system!r}; "
                "pyautogui requires Windows, Linux or macOS with a display."
            )
        # touch the optional deps so a missing install fails fast at load()
        _require_pyautogui()
        _require_pillow()
        self._loaded = True
        return True

    def unload(self) -> bool:
        self._loaded = False
        return True

    def get_capabilities(self) -> list[str]:
        return [
            "hermes.desktop.mouse_move",
            "hermes.desktop.mouse_click",
            "hermes.desktop.key_press",
            "hermes.desktop.type_text",
            "hermes.desktop.screenshot",
        ]

    # -- explicit tool registration (clean injection) --------------------- #
    def register_agent(self, agent_registry: Any) -> None:
        """Register the desktop_control Agent entity into ``agent_registry``."""
        from kernel.domain import Agent

        agent_registry.register(
            Agent(name="desktop_control", capabilities=["hermes.desktop"])
        )

    def register_tools(self, tool_registry: Any) -> None:
        """Register all desktop Tools into ``tool_registry`` (kernel service).

        Called by the kernel after ``load()`` (or by tests). Idempotent.
        """
        self._tool_registry = tool_registry
        for meta in self._tool_metas():
            tool = Tool(
                name=meta["name"],
                capability=meta["capability"],
                input_schema=meta["schema"],
            )
            try:
                tool_registry.register_sync(tool)
            except ValueError:
                pass  # already registered (idempotent)

    @staticmethod
    def _tool_metas() -> list[dict[str, Any]]:
        """Harvest @sdk.tool metadata from this class (name/capability/schema)."""
        from plugins.sdk.tool import get_tools

        return get_tools(DesktopControlPlugin)

    # -- tools ------------------------------------------------------------- #
    @sdk.tool(
        name="mouse_move",
        capability="hermes.desktop",
        schema={
            "type": "object",
            "properties": {"x": {"type": "integer"}, "y": {"type": "integer"}},
            "required": ["x", "y"],
        },
    )
    async def mouse_move(self, x: int, y: int) -> dict[str, Any]:
        """Move the cursor to (x, y)."""
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.moveTo, x, y)
        return {"ok": True}

    @sdk.tool(
        name="mouse_click",
        capability="hermes.desktop",
        schema={
            "type": "object",
            "properties": {
                "button": {"type": "string", "default": "left"},
                "clicks": {"type": "integer", "default": 1},
            },
        },
    )
    async def mouse_click(self, button: str = "left", clicks: int = 1) -> dict[str, Any]:
        """Click the mouse (default left button, single click)."""
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.click, button=button, clicks=clicks)
        return {"ok": True}

    @sdk.tool(
        name="key_press",
        capability="hermes.desktop",
        schema={
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    )
    async def key_press(self, key: str) -> dict[str, Any]:
        """Press a keyboard key (e.g. 'enter', 'a', 'ctrl+c')."""
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.press, key)
        return {"ok": True}

    @sdk.tool(
        name="type_text",
        capability="hermes.desktop",
        schema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "interval": {"type": "number", "default": 0.01},
            },
            "required": ["text"],
        },
    )
    async def type_text(self, text: str, interval: float = 0.01) -> dict[str, Any]:
        """Type ``text`` with ``interval`` seconds between keystrokes."""
        pyautogui = _require_pyautogui()
        await asyncio.to_thread(pyautogui.write, text, interval=interval)
        return {"ok": True}

    @sdk.tool(
        name="screenshot",
        capability="hermes.desktop",
        schema={
            "type": "object",
            "properties": {
                "region": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "[x, y, width, height] or null for full screen",
                }
            },
        },
    )
    async def screenshot(self, region: list[int] | None = None) -> dict[str, Any]:
        """Capture a PNG screenshot; return base64-encoded image data."""
        pyautogui = _require_pyautogui()
        _require_pillow()  # ensure Pillow is importable for downstream save
        region_tuple = tuple(region) if region else None
        img = await asyncio.to_thread(pyautogui.screenshot, region=region_tuple)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        encoded = base64.b64encode(buf.getvalue()).decode("utf-8")
        return {"image": encoded}
