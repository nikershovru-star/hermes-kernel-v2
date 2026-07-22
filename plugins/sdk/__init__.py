"""plugins/sdk/__init__.py — declarative Plugin SDK for Hermes Kernel v2.

The SDK lets plugin authors register Agents, Tools, Capabilities and event
handlers with plain decorators. Example:

    from plugins.sdk import sdk, configure_sdk
    from kernel.bus import EventBus
    from kernel.registry import (AgentRegistry, ToolRegistry, CapabilityRegistry)
    from kernel.domain import Document, Event

    configure_sdk(
        agent_registry=AgentRegistry(),
        tool_registry=ToolRegistry(),
        capability_registry=CapabilityRegistry(ToolRegistry()),
        bus=EventBus(),
    )

    @sdk.agent(name="researcher", capabilities=["hermes.search"])
    class Researcher:
        @sdk.tool(name="web_search", capability="hermes.search",
                  schema={"type": "object", "properties": {"q": {"type": "string"}}})
        async def search(self, q: str) -> list[Document]:
            ...

        @sdk.on_event("document.parsed")
        async def handle_doc(self, event: Event):
            ...

        @sdk.capability(name="hermes.custom", tools=["web_search"])
        def declare_custom(self):
            pass  # the decorator registers the Capability itself
"""

from .agent import agent, configure_sdk
from .capability import capability
from .event import on_event
from .tool import tool

__all__ = ["agent", "tool", "on_event", "capability", "configure_sdk"]


class _SDK:
    """Namespace facade so authors write `sdk.agent`, `sdk.tool`, etc."""

    agent = staticmethod(agent)
    tool = staticmethod(tool)
    on_event = staticmethod(on_event)
    capability = staticmethod(capability)
    configure = staticmethod(configure_sdk)


sdk = _SDK()
