"""plugins/sdk/agent.py — @agent decorator: registers Agent + harvests SDK members.

The decorator wires an agent class into the kernel: on construction it
registers the Agent in AgentRegistry, every @tool into ToolRegistry (as a Tool
whose handler is the bound method), every @capability into CapabilityRegistry,
and subscribes every @on_event method to the injected EventBus. All registration
is idempotent — re-constructing the agent does not duplicate registry entries.

Because __init__ runs on the main thread and the registries expose sync
register paths, injection is synchronous (no await needed in the constructor).
"""

from __future__ import annotations

from typing import Any, Callable

from kernel.domain import Agent, Capability, Tool
from kernel.registry import AgentRegistry, ToolRegistry
from kernel.capability import CapabilityRegistry

from .capability import get_capabilities
from .event import get_events
from .tool import get_tools

# Injected kernel services, set by `configure_sdk(...)` before decorating agents.
_REGISTRIES: dict[str, Any] = {}


def configure_sdk(
    *,
    agent_registry: AgentRegistry,
    tool_registry: ToolRegistry,
    capability_registry: CapabilityRegistry,
    bus: Any,
) -> None:
    """Inject the kernel services the SDK decorators will use."""
    _REGISTRIES["agent"] = agent_registry
    _REGISTRIES["tool"] = tool_registry
    _REGISTRIES["capability"] = capability_registry
    _REGISTRIES["bus"] = bus


def _require(name: str) -> Any:
    svc = _REGISTRIES.get(name)
    if svc is None:
        raise RuntimeError(
            f"SDK not configured: call configure_sdk() before using @agent "
            f"(missing {name})"
        )
    return svc


def agent(name: str, capabilities: list[str] | None = None) -> Callable:
    """Class decorator: turn a class into a kernel-registered Agent."""

    def decorate(cls) -> type:
        ar: AgentRegistry = _require("agent")
        tr: ToolRegistry = _require("tool")
        cr: CapabilityRegistry = _require("capability")
        bus = _require("bus")

        original_init = cls.__init__

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            # services available on the instance (test 5: Agent().__bus__)
            self.__bus__ = bus
            self.__tool_registry__ = tr
            self.__capability_registry__ = cr

            # 1) register the Agent itself (idempotent)
            agent_entity = Agent(name=name, capabilities=capabilities or [])
            ar.register(agent_entity)
            self.__agent_entity__ = agent_entity

            # 2) harvest @tool -> ToolRegistry (handler = bound method)
            for meta in get_tools(cls):
                t = Tool(
                    name=meta["name"],
                    capability=meta["capability"],
                    input_schema=meta["schema"],
                )
                try:
                    tr.register_sync(t)
                except ValueError:
                    pass  # idempotent: already registered
                setattr(self, f"__tool_handler__{meta['name']}",
                        getattr(self, meta["method"]))

            # 3) harvest @capability -> CapabilityRegistry
            for meta in get_capabilities(cls):
                cap = Capability(name=meta["name"], tools=meta["tools"])
                try:
                    cr.register_sync(cap)
                except ValueError:
                    pass  # idempotent

            # 4) harvest @on_event -> subscribe to EventBus
            for etype, method in get_events(cls):
                bound = getattr(self, method.__name__)
                bus.subscribe(etype, bound)

            # run user __init__ last so their setup sees the injected services
            if original_init is not object.__init__:
                original_init(self, *args, **kwargs)

        cls.__init__ = __init__
        return cls

    return decorate
