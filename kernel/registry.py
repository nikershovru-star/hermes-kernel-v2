"""kernel/registry.py — in-memory PluginRegistry + ToolRegistry for Hermes v2.

AXIS CONTRACT: imports only kernel.domain (+ defines PluginManifest here, which
loader.py imports). No I/O — file loading belongs to plugins/loader.py.

Both registries are concurrency-safe via asyncio.Lock. They store pure data
(manifests / Tool entities); behaviour lives in the plugin instances and the
kernel, not here.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from kernel.bus import EventBus
from kernel.domain import Agent, Event, PluginManifest, Tool

logger = logging.getLogger("hermes.kernel.registry")  # type: ignore[name-defined]


@dataclass(frozen=True)
class PluginInfo:
    """Immutable snapshot of a loaded (or disabled) plugin."""

    name: str
    version: str
    capabilities: tuple[str, ...]
    entrypoint: str
    status: str  # "loaded" | "disabled"


class PluginRegistry:
    """plugin_id -> (manifest, instance). Capability-indexed lookup."""

    def __init__(self, bus: EventBus | None = None) -> None:
        self._plugins: dict[str, tuple[PluginManifest, Any]] = {}
        self._disabled: set[str] = set()
        self._bus = bus
        self._lock = asyncio.Lock()

    async def register(
        self, manifest: PluginManifest, instance: Any
    ) -> str:
        async with self._lock:
            pid = manifest.plugin_id
            if pid in self._plugins:
                raise ValueError(f"plugin '{pid}' already registered")
            if not manifest.entrypoint:
                raise ValueError(f"plugin '{pid}' has empty entrypoint")
            self._plugins[pid] = (manifest, instance)
            self._disabled.discard(pid)
            return pid

    def register_sync(self, manifest: PluginManifest, instance: Any) -> str:
        """Sync variant for CLI / non-event-loop callers."""
        pid = manifest.plugin_id
        if pid in self._plugins:
            raise ValueError(f"plugin '{pid}' already registered")
        if not manifest.entrypoint:
            raise ValueError(f"plugin '{pid}' has empty entrypoint")
        self._plugins[pid] = (manifest, instance)
        self._disabled.discard(pid)
        return pid

    def list_plugins(self) -> list[PluginInfo]:
        """Return a snapshot of every known plugin (loaded or disabled)."""
        infos: list[PluginInfo] = []
        for pid, (manifest, _inst) in self._plugins.items():
            status = "disabled" if pid in self._disabled else "loaded"
            infos.append(
                PluginInfo(
                    name=pid,
                    version=manifest.version,
                    capabilities=tuple(manifest.capabilities),
                    entrypoint=manifest.entrypoint,
                    status=status,
                )
            )
        return infos

    def get_sync(self, plugin_id: str) -> Any | None:
        """Return the live instance, or None if unknown/disabled."""
        if plugin_id in self._disabled:
            return None
        entry = self._plugins.get(plugin_id)
        return entry[1] if entry else None

    def is_disabled(self, plugin_id: str) -> bool:
        return plugin_id in self._disabled

    def disable(self, plugin_id: str) -> bool:
        """Unload *plugin_id* from runtime: mark disabled, drop its entrypoint
        module from sys.modules, and publish ``plugin.disabled``.

        Returns True if the plugin existed.
        """
        entry = self._plugins.get(plugin_id)
        if entry is None:
            logger.warning("disable: unknown plugin %s", plugin_id)
            return False
        manifest, _inst = entry
        module_name = manifest.entrypoint.split(":", 1)[0]
        if module_name in sys.modules:
            del sys.modules[module_name]
            logger.info("disable: unloaded module %s", module_name)
        self._disabled.add(plugin_id)
        if self._bus is not None:
            # sync method may run outside an event loop (CLI). Guard so we
            # never call create_task without a running loop (see ADR-010).
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                logger.info(
                    "disable: event loop not running; skipping publish of "
                    "plugin.disabled for %s", plugin_id
                )
            else:
                self._bus.publish(
                    Event(type="plugin.disabled", source="plugin.registry",
                          payload={"name": plugin_id})
                )
        logger.info("disable: %s marked disabled", plugin_id)
        return True

    def enable(self, plugin_id: str) -> bool:
        """Clear the disabled marker so the plugin is loaded again."""
        if plugin_id in self._plugins:
            self._disabled.discard(plugin_id)
            return True
        return False

    def load_paths(self, paths: list[Any]) -> list[Any]:
        """Scan + load every plugin under *paths* (via plugins.loader)."""
        from pathlib import Path

        from plugins.loader import auto_load

        loaded = auto_load([Path(str(p)) for p in paths])
        for inst in loaded:
            self.register_sync(inst.manifest, inst)
        return loaded

    async def unregister(self, plugin_id: str) -> bool:
        async with self._lock:
            return self._plugins.pop(plugin_id, None) is not None

    async def get(self, plugin_id: str) -> tuple[PluginManifest, Any] | None:
        async with self._lock:
            return self._plugins.get(plugin_id)

    async def list(self) -> list[PluginManifest]:
        async with self._lock:
            return [m for m, _ in self._plugins.values()]

    async def get_by_capability(self, capability: str) -> list[Any]:
        async with self._lock:
            return [
                inst
                for m, inst in self._plugins.values()
                if capability in m.capabilities
            ]

    def clear(self) -> None:
        """Drop all plugins + disabled markers (test isolation)."""
        self._plugins.clear()
        self._disabled.clear()


class ToolRegistry:
    """tool_id -> Tool. Capability-discoverable."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        self._lock = asyncio.Lock()

    def register_sync(self, tool: Tool) -> str:
        if tool.id in self._tools:
            raise ValueError(f"tool '{tool.id}' already registered")
        if not tool.capability:
            raise ValueError(f"tool '{tool.id}' has empty capability")
        self._tools[tool.id] = tool
        return tool.id

    def get_by_name_sync(self, name: str) -> Tool | None:
        for t in self._tools.values():
            if t.name == name:
                return t
        return None

    async def register(self, tool: Tool) -> str:
        async with self._lock:
            return self.register_sync(tool)

    async def unregister(self, tool_id: str) -> bool:
        async with self._lock:
            return self._tools.pop(tool_id, None) is not None

    async def get(self, tool_id: str) -> Tool | None:
        async with self._lock:
            return self._tools.get(tool_id)

    async def get_by_name(self, name: str) -> Tool | None:
        async with self._lock:
            return self.get_by_name_sync(name)

    async def list(self) -> list[Tool]:
        async with self._lock:
            return list(self._tools.values())

    async def discover(self, capability: str) -> list[Tool]:
        async with self._lock:
            # prefix match: "hermes.search" discovers "hermes.search.pdf" etc.
            return [
                t
                for t in self._tools.values()
                if t.capability == capability or t.capability.startswith(capability + ".")
            ]


class AgentRegistry:
    """agent_id -> Agent. Sync (agents are constructed on the main thread)."""

    def __init__(self) -> None:
        self._agents: dict[str, Agent] = {}

    def register(self, agent: Agent) -> str:
        if agent.id in self._agents:
            raise ValueError(f"agent '{agent.id}' already registered")
        self._agents[agent.id] = agent
        return agent.id

    def get(self, id: str) -> Agent | None:
        return self._agents.get(id)

    def get_by_name(self, name: str) -> Agent | None:
        for a in self._agents.values():
            if a.name == name:
                return a
        return None

    def list(self) -> list[Agent]:
        return list(self._agents.values())

    def unregister(self, id: str) -> bool:
        return self._agents.pop(id, None) is not None
