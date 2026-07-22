"""tests/test_sdk.py — Plugin SDK decorators + AgentRegistry wiring."""

import asyncio

import pytest

from kernel.bus import EventBus
from kernel.capability import CapabilityRegistry
from kernel.domain import Event
from kernel.registry import AgentRegistry, ToolRegistry
from plugins.sdk import agent, capability, configure_sdk, on_event, sdk, tool


@pytest.fixture
def sdk_env():
    bus = EventBus()
    ar = AgentRegistry()
    tr = ToolRegistry()
    cr = CapabilityRegistry(tr)
    configure_sdk(agent_registry=ar, tool_registry=tr, capability_registry=cr, bus=bus)
    return bus, ar, tr, cr


async def test_agent_decorator_registers(sdk_env) -> None:
    _, ar, _, _ = sdk_env

    @agent(name="researcher", capabilities=["hermes.search"])
    class Researcher:
        pass

    Researcher()
    assert ar.get_by_name("researcher") is not None
    assert ar.get_by_name("researcher").capabilities == ["hermes.search"]


async def test_tool_decorator_registers(sdk_env) -> None:
    _, ar, tr, _ = sdk_env

    @agent(name="t")
    class T:
        @tool(name="web_search", capability="hermes.search",
              schema={"type": "object", "properties": {"q": {"type": "string"}}})
        async def search(self, q: str):
            return []

    T()
    t = await tr.get_by_name("web_search")
    assert t is not None
    assert t.capability == "hermes.search"
    assert t.input_schema["properties"]["q"]["type"] == "string"


async def test_event_decorator_subscribes(sdk_env) -> None:
    bus, ar, tr, cr = sdk_env
    seen = []

    @agent(name="ev")
    class Ev:
        @on_event("document.parsed")
        async def handle_doc(self, event: Event):
            seen.append(event.payload)

    Ev()
    fut = bus.wait_for(["document.parsed"])
    bus.publish(Event(type="document.parsed", payload={"id": 1}))
    evt = await asyncio.wait_for(fut, timeout=2.0)
    assert seen == [{"id": 1}]
    assert evt.type == "document.parsed"


async def test_capability_decorator_registers(sdk_env) -> None:
    _, ar, tr, cr = sdk_env

    @agent(name="cap")
    class Cap:
        @capability(name="hermes.custom", tools=["web_search"])
        def declare(self):
            pass

    Cap()
    cap = await cr.get_by_name("hermes.custom")
    assert cap is not None
    assert cap.tools == ["web_search"]


async def test_agent_injected_registries(sdk_env) -> None:
    bus, ar, tr, cr = sdk_env

    @agent(name="inj")
    class Inj:
        pass

    a = Inj()
    assert a.__bus__ is bus
    assert a.__tool_registry__ is tr
    assert a.__capability_registry__ is cr
