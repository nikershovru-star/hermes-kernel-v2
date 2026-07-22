"""kernel/registry.py — in-memory PluginRegistry + ToolRegistry for Hermes v2.

AXIS CONTRACT: imports only kernel.domain (+ defines PluginManifest here, which
loader.py imports). No I/O — file loading belongs to plugins/loader.py.

Both registries are concurrency-safe via asyncio.Lock. They store pure data
(manifests / Tool entities); behaviour lives in the plugin instances and the
kernel, not here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from pydantic import BaseModel, Field

from kernel.domain import Agent, PluginManifest, Tool


class PluginRegistry:
    """plugin_id -> (manifest, instance). Capability-indexed lookup."""

    def __init__(self) -> None:
        self._plugins: dict[str, tuple[PluginManifest, _PluginInstance]] = {}
        self._lock = asyncio.Lock()

    async def register(
        self, manifest: PluginManifest, instance: _PluginInstance
    ) -> str:
        async with self._lock:
            pid = manifest.plugin_id
            if pid in self._plugins:
                raise ValueError(f"plugin '{pid}' already registered")
            if not manifest.entrypoint:
                raise ValueError(f"plugin '{pid}' has empty entrypoint")
            self._plugins[pid] = (manifest, instance)
            return pid

    async def unregister(self, plugin_id: str) -> bool:
        async with self._lock:
            return self._plugins.pop(plugin_id, None) is not None

    async def get(self, plugin_id: str) -> tuple[PluginManifest, _PluginInstance] | None:
        async with self._lock:
            return self._plugins.get(plugin_id)

    async def list(self) -> list[PluginManifest]:
        async with self._lock:
            return [m for m, _ in self._plugins.values()]

    async def get_by_capability(self, capability: str) -> list[_PluginInstance]:
        async with self._lock:
            return [
                inst
                for m, inst in self._plugins.values()
                if capability in m.capabilities
            ]


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
