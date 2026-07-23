"""kernel/discovery.py — capability handler auto-discovery (ADR-018).

Implements the deferred auto-discovery step from ADR-017: instead of manually
wiring every plugin/agent into the ``CapabilityExecutor`` (Decision D in v2.3.0),
the kernel reflects over already-loaded **instances** and registers their
capability handlers automatically.

AXIS CONTRACT: depends ONLY on kernel.domain, kernel.agent, kernel.capability,
the @sdk.tool marker attribute directly (string key, no plugin import). It NEVER imports — it
operates on instances the kernel already holds, so the kernel -> plugins axis
stays clean. This is the crucial difference vs. module scanning: discovery is a
post-load reflection step, not an import-time crawl.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any

from kernel.agent import BaseAgent

logger = logging.getLogger("hermes.kernel.discovery")


def discover_handlers(instances: list[Any], executor: Any) -> int:
    """Reflect over ``instances`` and register their capability handlers.

    For each instance:
    * ``BaseAgent`` -> delegated to ``executor.register_agent`` (reuses the
      v2.3.0 agent wiring: capability becomes a Task-routing handler).
    * Any other object -> methods marked with ``@sdk.tool`` become handlers
      keyed by their declared ``capability``. The handler adapts the plugin's
      async tool method (``method(**params)``) to the executor's
      ``handler(params, context)`` signature.

    Returns the number of capabilities newly wired. Idempotent: re-running on
    the same instances is a no-op (register overwrites by capability name).
    """
    wired = 0
    for inst in instances:
        if isinstance(inst, BaseAgent):
            executor.register_agent(inst)
            wired += len(inst.capabilities)
            continue
        metas = _scan_sdk_tools(type(inst))
        for meta in metas:
            cap = meta["capability"]
            method_name = meta["method"]
            executor.register_handler(cap, _make_plugin_handler(inst, method_name))
            wired += 1
            logger.debug("auto-wired capability %s -> %s.%s", cap, type(inst).__name__, method_name)
    return wired


def _make_plugin_handler(inst: Any, method_name: str) -> Any:
    """Adapt a plugin's ``@sdk.tool`` async method to executor handler signature."""
    method = getattr(inst, method_name)

    async def handler(params: dict[str, Any], context: dict[str, Any] | None) -> Any:
        kwargs = dict(params or {})
        # drop executor-only reserved keys the plugin method does not expect
        sig = inspect.signature(method)
        if "context" in sig.parameters and context is not None:
            kwargs["context"] = context
        return await method(**kwargs)

    return handler


async def discover_handlers_async(instances: list[Any], executor: Any) -> int:
    """Async variant (kept for symmetry with async kernel bootstrap)."""
    return await asyncio.to_thread(discover_handlers, instances, executor)


_TOOL_MARKER = "__sdk_tool__"  # mirrors plugins.sdk.tool._TOOL_MARKER (no import)


def _scan_sdk_tools(cls: type) -> list[dict]:
    """Harvest @sdk.tool metadata from a class WITHOUT importing plugins.

    Reads the marker attribute directly (string key) so ``kernel`` never
    imports ``plugins`` (axis contract). Mirrors ``plugins.sdk.tool.get_tools``.
    """
    found: list[dict] = []
    for attr in vars(cls).values():
        meta = getattr(attr, _TOOL_MARKER, None)
        if meta is not None:
            found.append(meta)
    return found


__all__ = ["discover_handlers", "discover_handlers_async"]
