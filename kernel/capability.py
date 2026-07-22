"""kernel/capability.py — CapabilityRegistry (async, in-memory).

AXIS CONTRACT: imports only kernel.domain (Capability, Tool) and
kernel.registry (ToolRegistry). Resolves a capability -> its bundled tools
via the injected ToolRegistry. Mirrors ToolRegistry's async/lock design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from kernel.domain import Capability, Tool
from kernel.registry import ToolRegistry

logger = logging.getLogger(__name__)


class CapabilityRegistry:
    """Async registry mapping capability id -> Capability.

    Tools are resolved lazily via an injected ToolRegistry, so a Capability
    is just a declarative grouping (name + list of Tool.names). Missing tools
    are skipped with a warning (at-least-one intent, never crash resolution).
    """

    def __init__(self, tool_registry: ToolRegistry) -> None:
        self._tools = tool_registry
        self._caps: dict[str, Capability] = {}
        self._lock = asyncio.Lock()

    def register_sync(self, capability: Capability) -> str:
        if any(c.name == capability.name for c in self._caps.values()):
            raise ValueError(
                f"Capability name {capability.name!r} already registered"
            )
        self._caps[capability.id] = capability
        return capability.id

    async def register(self, capability: Capability) -> str:
        """Register a capability; returns its id. Duplicate name -> ValueError."""
        async with self._lock:
            return self.register_sync(capability)

    async def get(self, id: str) -> Optional[Capability]:
        async with self._lock:
            return self._caps.get(id)

    async def get_by_name(self, name: str) -> Optional[Capability]:
        async with self._lock:
            return self.get_by_name_sync(name)

    def get_by_name_sync(self, name: str) -> Optional[Capability]:
        for c in self._caps.values():
            if c.name == name:
                return c
        return None

    async def resolve_tools(self, capability_id: str) -> list[Tool]:
        """Return tools bundled by a capability, resolving Tool.names via ToolRegistry.

        Unknown tool names are skipped with a logger.warning (do not crash).
        """
        cap = await self.get(capability_id)
        if cap is None:
            return []
        resolved: list[Tool] = []
        for tool_name in cap.tools:
            tool = await self._tools.get_by_name(tool_name)
            if tool is None:
                logger.warning(
                    "Capability %s references missing tool %r", cap.name, tool_name
                )
                continue
            resolved.append(tool)
        return resolved

    async def resolve_tools_by_name(self, name: str) -> list[Tool]:
        """Resolve a capability's tools by its name (convenience for Executor)."""
        cap = await self.get_by_name(name)
        if cap is None:
            return []
        return await self.resolve_tools(cap.id)

    async def discover(self, prefix: str) -> list[Capability]:
        """Prefix-match by capability.name. Empty prefix -> all capabilities."""
        async with self._lock:
            if not prefix:
                return list(self._caps.values())
            return [
                c
                for c in self._caps.values()
                if c.name == prefix or c.name.startswith(prefix + ".")
            ]

    async def unregister(self, id: str) -> bool:
        async with self._lock:
            return self._caps.pop(id, None) is not None

    async def list(self) -> list[Capability]:
        async with self._lock:
            return list(self._caps.values())
