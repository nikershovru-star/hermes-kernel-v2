"""kernel/capability.py — CapabilityRegistry (async, in-memory).

AXIS CONTRACT: imports only kernel.domain (Capability, Tool) and
kernel.registry (ToolRegistry). Resolves a capability -> its bundled tools
via the injected ToolRegistry. Mirrors ToolRegistry's async/lock design.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from kernel.agent import BaseAgent
from kernel.domain import Artifact, Capability, Task, Tool
from kernel.discovery import discover_handlers
from kernel.health import CircuitBreaker
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

class CapabilityExecutor:
    """Unified, namespaced capability dispatch (ADR-016).

    Resolves a capability string ("browser.navigate", "desktop.click") to an
    injected async handler and returns a unified Artifact. The executor does NOT
    import plugins — handlers are injected by the kernel, which gathers them from
    plugin/agent instances (keeps the kernel -> plugins axis intact).

    A handler signature is `async def handler(params: dict, context: dict | None)
    -> Any`. Its return value is normalized into an Artifact:

    * Artifact returned as-is (provenance appended).
    * dict with content/type/format keys -> mapped onto Artifact.
    * any other value -> wrapped as Artifact(type="result", content=value).
    """

    def __init__(
        self,
        handlers: dict[str, Any] | None = None,
        capability_registry: "CapabilityRegistry | None" = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        # capability_name -> async handler(params, context) -> Any
        self._handlers: dict[str, Any] = dict(handlers or {})
        self._caps = capability_registry
        self._cb = circuit_breaker

    def register_handler(self, capability: str, handler: Any) -> None:
        """Register (or override) the handler for a namespaced capability."""
        self._handlers[capability] = handler

    def register_agent(self, agent: "BaseAgent") -> None:
        """Wire a BaseAgent's capabilities into the executor (dogfood).

        Each capability becomes a handler that builds a ``Task`` from the call
        params/context and delegates to ``agent.execute(agent_id, task)``,
        returning the resulting ``Artifact``. This is the manual wiring step of
        ADR-017 (auto-discovery is deferred to ADR-018).
        """
        for cap in agent.capabilities:
            self._handlers[cap] = self._make_agent_handler(agent, cap)

    @staticmethod
    def _make_agent_handler(agent: "BaseAgent", capability: str) -> Any:
        async def handler(params: dict[str, Any], context: dict[str, Any] | None) -> Artifact:
            meta = dict(params or {})
            if context:
                meta.update({k: v for k, v in context.items() if k not in meta})
            task = Task(name=capability, capability=capability, metadata=meta)
            return await agent.execute(agent.agent_id, task)
        return handler

    def autodiscover(self, instances: list[Any]) -> int:
        """Auto-wire capability handlers from already-loaded plugin/agent instances (ADR-018).

        Replaces the manual ``register_agent`` / ``register_handler`` calls with
        reflection: BaseAgent instances are wired via ``register_agent``; other
        instances have their ``@sdk.tool``-marked methods registered by capability.
        The kernel passes the instances it already loaded (no plugin import), so
        the kernel -> plugins axis stays intact.
        """
        return discover_handlers(instances, self)

    async def execute(
        self,
        capability: str,
        params: dict[str, Any],
        context: dict[str, Any] | None = None,
    ) -> Artifact:
        """Dispatch ``capability`` with ``params``/``context``; return Artifact."""
        handler = self._handlers.get(capability)
        if handler is None and self._caps is not None:
            cap = await self._caps.get_by_name(capability)
            if cap is None:
                raise KeyError(f"no handler or capability registered for {capability!r}")
            raise KeyError(
                f"capability {capability!r} declared but no handler injected"
            )
        if handler is None:
            raise KeyError(f"no handler registered for capability {capability!r}")

        if self._cb is not None:
            result = await self._cb.call(capability, handler(params, context))
        else:
            result = await handler(params, context)
        return self._normalize(result, capability)

    @staticmethod
    def _normalize(result: Any, capability: str) -> Artifact:
        """Coerce a handler return value into a unified Artifact."""
        if isinstance(result, Artifact):
            artifact = result
        elif isinstance(result, dict) and ("content" in result or "type" in result):
            artifact = Artifact(
                type=result.get("type", "result"),
                content=result.get("content"),
                format=result.get("format", "json"),
                source=result.get("source", f"capability:{capability}"),
                provenance=list(result.get("provenance", [])),
            )
        else:
            artifact = Artifact(
                type="result",
                content=result,
                format="json",
                source=f"capability:{capability}",
            )
        artifact.provenance = artifact.provenance + [f"cap:{capability}"]
        return artifact


__all__ = ["CapabilityRegistry", "CapabilityExecutor", "discover_handlers"]
