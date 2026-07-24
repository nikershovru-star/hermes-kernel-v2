"""tests/test_marketplace_integration.py — marketplace + AgentRuntime/WorkflowEngine (ADR-026)."""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor, CapabilityRegistry
from kernel.domain import Agent, Artifact, Task, Workflow, WorkflowInstance, WorkflowStatus, WorkflowStep, WorkflowTrigger
from kernel.events import EventBus, EventStore
from kernel.marketplace import PluginMarketplace
from kernel.marketplace_domain import PluginPackage, PluginSource, PluginStatus
from kernel.marketplace_store import MarketplaceStore
from kernel.workflow import WorkflowEngine


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)

    async def start(self) -> str:
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(type=task.capability, content={"ok": True}, format="json", source="a")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running"}


class _MockHTTP:
    def __init__(self, catalog):
        self._catalog = catalog

    async def get(self, url):
        return json.dumps(self._catalog)


def _mp_with_catalog(store=None):
    http = _MockHTTP([{"name": "vision", "package_id": "pkg.vision", "version": "1.0", "capabilities": ["img.classify"], "checksum": None}])
    return PluginMarketplace(event_bus=EventBus(), event_store=EventStore(), store=store, rng=random.Random(2), http_client=http)


async def test_agent_runtime_install_capability() -> None:
    mp = _mp_with_catalog()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), marketplace=mp)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    await mp.discover("http://catalog")
    pkg = await rt.install_capability("a", "pkg.vision")
    assert pkg.status == PluginStatus.INSTALLED


async def test_agent_runtime_install_capability_registers_in_executor() -> None:
    store = MarketplaceStore()
    mp = _mp_with_catalog(store=store)
    bus, evstore = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=evstore, marketplace=mp)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    reg = CapabilityRegistry(tool_registry=None)
    await mp.discover("http://catalog")
    await rt.install_capability("a", "pkg.vision", capability_registry=reg)
    names = {c.name for c in await reg.list()}
    assert "img.classify" in names


async def test_agent_runtime_install_capability_no_marketplace() -> None:
    rt = AgentRuntime(bus=EventBus(), store=EventStore())
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    with pytest.raises(RuntimeError):
        await rt.install_capability("a", "pkg.x")


async def test_workflow_discover_plugins() -> None:
    mp = _mp_with_catalog()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), marketplace=mp)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, EventBus(), EventStore(), marketplace=mp)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    await mp.discover("http://catalog")
    found = await eng.discover_plugins("img.classify")
    assert any(p.package_id == "pkg.vision" for p in found)


async def test_workflow_discover_plugins_no_marketplace() -> None:
    bus, evstore = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=evstore)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, evstore)  # no marketplace
    assert await eng.discover_plugins("cap.x") == []


async def test_plugin_installed_event_via_integration() -> None:
    store = EventStore()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    pkg = PluginPackage(package_id="p.local", name="local", version="1.0", source=PluginSource.LOCAL, entrypoint="plugins.l:L", capabilities=["cap.l"])
    await mp.install(pkg)
    assert any(e.type == "mp.plugin_installed" for e in store._events)


async def test_plugin_install_failed_event_via_integration() -> None:
    store = EventStore()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    bad = PluginPackage(package_id="b", name="b", version="1", source=PluginSource.REMOTE, entrypoint="x", checksum="deadbeef")
    await mp.install(bad)
    assert any(e.type == "mp.plugin_install_failed" for e in store._events)


async def test_store_roundtrip_package() -> None:
    store = MarketplaceStore()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=EventStore(), store=store, rng=random.Random(1))
    await mp.install(_pkg())
    assert store.get_package("pkg.x") is not None
    assert store.get_package("pkg.x").status == PluginStatus.INSTALLED


def _pkg():
    return PluginPackage(package_id="pkg.x", name="x", version="1.0", source=PluginSource.LOCAL, entrypoint="plugins.x:X", capabilities=["cap.x"])


async def test_backward_compat_agent_runtime_without_marketplace() -> None:
    rt = AgentRuntime(bus=EventBus(), store=EventStore())
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    aid = await rt.start(agent)
    assert rt.get(aid) is not None


async def test_backward_compat_workflow_engine_without_marketplace() -> None:
    bus, evstore = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=evstore)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, evstore)
    assert eng._mp is None
