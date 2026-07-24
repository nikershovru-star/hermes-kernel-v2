"""Integration tests: AgentRuntime / WorkflowEngine / PluginMarketplace + guard (ADR-028)."""

from __future__ import annotations

import asyncio

import pytest

from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.capability_guard import CapabilityGuard, PermissionDeniedError
from kernel.domain import Task, Workflow, WorkflowStep
from kernel.events import EventBus, EventStore
from kernel.marketplace import PluginMarketplace
from kernel.marketplace_domain import PluginPackage, PluginSource, PluginStatus
from kernel.security_domain import Permission, SandboxPolicy
from kernel.security_store import SecurityStore
from kernel.workflow import WorkflowEngine


def _pkg(pid, caps, policy=None):
    return PluginPackage(
        package_id=pid, name=pid, version="1.0", source=PluginSource.LOCAL,
        entrypoint=f"plugins.{pid}", capabilities=list(caps), policy=policy,
    )


# -- marketplace.install registers policy ------------------------------ #
async def test_marketplace_install_registers_policy():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    pkg = _pkg("weather", ["weather.fetch"],
               policy=SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:weather.fetch")]))
    res = await mp.install(pkg)
    assert res.status == PluginStatus.INSTALLED
    assert "weather" in guard.get_policies()
    assert guard.check("weather", "execute", "plugin:weather.fetch") is True


async def test_marketplace_validate_rejects_disallowed_action():
    mp = PluginMarketplace()
    bad = _pkg("evil", ["evil.run"],
               policy=SandboxPolicy(permissions=[Permission(action="rm-rf", resource="*")]))
    ok, reason = mp.validate_package(bad)
    assert ok is False and "disallowed" in reason


async def test_marketplace_no_guard_zero_regression():
    # guard=None -> install path unchanged, no policy registration attempted
    mp = PluginMarketplace()
    res = await mp.install(_pkg("weather", ["weather.fetch"],
                                policy=SandboxPolicy(permissions=[Permission(action="execute", resource="*")])))
    assert res.status == PluginStatus.INSTALLED


# -- AgentRuntime.execute with guard (allowed + denied) --------------- #
class _FakeAgent(BaseAgent):
    def __init__(self, name="a", caps=None):
        from kernel.domain import Agent as AgentEntity
        super().__init__(AgentEntity(id="agent-" + name, name=name, capabilities=caps or []))
        self._started = False

    async def start(self) -> str:
        self._started = True
        return self.agent_id

    async def execute(self, agent_id, task):
        return f"ran:{task.capability}"

    async def stop(self, agent_id) -> bool:
        self._started = False
        return True

    async def status(self, agent_id) -> dict:
        return {"state": "running" if self._started else "offline"}


async def test_agentruntime_guard_wraps_allowed():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    pkg = _pkg("weather", ["weather.fetch"],
               policy=SandboxPolicy(permissions=[Permission(action="execute", resource="capability:weather.fetch")]))
    await mp.install(pkg)
    rt = AgentRuntime(marketplace=mp, guard=guard)
    agent = _FakeAgent(caps=["weather.fetch"])
    aid = await rt.start(agent)
    result = await rt.execute(aid, Task(name="t", capability="weather.fetch"))
    assert result == "ran:weather.fetch"


async def test_agentruntime_guard_denies():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    pkg = _pkg("weather", ["weather.fetch"],
               policy=SandboxPolicy(permissions=[Permission(action="execute", resource="capability:weather.fetch")]))
    await mp.install(pkg)
    # the denied capability must belong to an installed, guarded package
    await mp.install(_pkg("secret", ["secret.spy"],
                       policy=SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:secret")])))
    rt = AgentRuntime(marketplace=mp, guard=guard)
    agent = _FakeAgent(caps=["weather.fetch"])
    aid = await rt.start(agent)
    # capability not granted (policy allows plugin:secret, not capability:secret.spy) -> deny
    with pytest.raises(PermissionDeniedError):
        await rt.execute(aid, Task(name="t", capability="secret.spy"))


async def test_agentruntime_install_capability_registers_policy():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    rt = AgentRuntime(marketplace=mp, guard=guard)
    agent = _FakeAgent(caps=["weather.fetch"])
    await rt.start(agent)
    # register the package in the marketplace, then install via the runtime
    pkg = _pkg("weather", ["weather.fetch"],
               policy=SandboxPolicy(permissions=[Permission(action="execute", resource="capability:weather.fetch")]))
    await mp.install(pkg)
    installed = await rt.install_capability(agent.agent_id, "weather")
    assert "weather" in guard.get_policies()


# -- WorkflowEngine.discover_plugins filtered by guard ----------------- #
async def test_workflow_discover_plugins_filtered_by_guard():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    await mp.install(_pkg("weather", ["weather.fetch"],
                       policy=SandboxPolicy(permissions=[Permission(action="discover", resource="plugin:weather")])))
    await mp.install(_pkg("secret", ["secret.spy"],
                       policy=SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:secret")])))
    # secret has no "discover" permission -> filtered out
    bus, store = EventBus(), EventStore()
    wf = WorkflowEngine(AgentRuntime(marketplace=mp), CapabilityExecutor(), bus, store, marketplace=mp, guard=guard)
    found = await wf.discover_plugins("fetch")
    ids = {p.package_id for p in found}
    assert "weather" in ids
    assert "secret" not in ids


async def test_workflow_discover_no_guard_returns_all():
    mp = PluginMarketplace()
    await mp.install(_pkg("weather", ["weather.fetch"]))
    bus, store = EventBus(), EventStore()
    wf = WorkflowEngine(AgentRuntime(marketplace=mp), CapabilityExecutor(), bus, store, marketplace=mp)
    found = await wf.discover_plugins("fetch")
    assert {p.package_id for p in found} == {"weather"}


# -- WorkflowEngine.execute_adaptive permission denial -> FAIL -------- #
async def test_workflow_step_permission_denied_fails():
    guard = CapabilityGuard()
    mp = PluginMarketplace(guard=guard)
    await mp.install(_pkg("secret", ["secret.spy"],
                       policy=SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:secret")])))
    rt = AgentRuntime(marketplace=mp, guard=guard)
    bus, store = EventBus(), EventStore()
    wf = WorkflowEngine(rt, CapabilityExecutor(), bus, store, marketplace=mp, guard=guard)
    inst = await wf.start(Workflow(id="w1", name="w", steps=[WorkflowStep(id="s1", capability="secret.spy", name="s1")]))
    artifacts = await wf.execute_adaptive(inst.id, Workflow(id="w1", name="w", steps=[WorkflowStep(id="s1", capability="secret.spy", name="s1")]))
    assert any(a.type == "error" and a.content.get("reason") == "permission_denied" for a in artifacts)
    assert inst.context.get("permission_denied")
    # honest fix: the guard sets the instance to FAILED (no infinite loop)
    assert inst.status.value == "failed"


# -- audit events emitted on bus during denied workflow step ---------- #
async def test_workflow_denied_emits_audit_event():
    bus = EventBus()
    store = EventStore()
    guard = CapabilityGuard(event_bus=bus, event_store=store)
    mp = PluginMarketplace(guard=guard)
    await mp.install(_pkg("secret", ["secret.spy"],
                       policy=SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:secret")])))
    rt = AgentRuntime(marketplace=mp, guard=guard)
    wf = WorkflowEngine(rt, CapabilityExecutor(), bus, store, marketplace=mp, guard=guard)
    inst = await wf.start(Workflow(id="w1", name="w", steps=[WorkflowStep(id="s1", capability="secret.spy", name="s1")]))
    await wf.execute_adaptive(inst.id, Workflow(id="w1", name="w", steps=[WorkflowStep(id="s1", capability="secret.spy", name="s1")]))
    all_ev = await store.read_all()
    assert any(e.type == "sec.audit_entry" for e in all_ev)
