"""tests/test_sandbox_integration.py — Sandbox wired into runtime components (ADR-020).

Verifies that AgentRuntime.execute and WorkflowEngine.execute_step honour a
Sandbox: tight timeout breaches + triggers cleanup (agent.stop / workflow
compensation), and that the components still work WITHOUT a sandbox (backward
compat — existing tests must keep passing).
"""

from __future__ import annotations

import asyncio

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.bus import EventBus
from kernel.capability import CapabilityExecutor
from kernel.workflow import WorkflowEngine
from kernel.domain import Agent, Artifact, SandboxPolicy, Task, Workflow, WorkflowInstance, WorkflowStep, WorkflowStatus
from kernel.events import EventStore, SandboxViolationEvent
from kernel.sandbox import Sandbox, SandboxTimeoutError


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent, delay: float = 0.0) -> None:
        super().__init__(entity)
        self._running = False
        self.delay = delay
        self.stops = 0

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        self.stops += 1
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        if self.delay:
            await asyncio.sleep(self.delay)
        return Artifact(type=task.capability, content={"ok": True}, format="json")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running" if self._running else "stopped"}


def _build_sandbox() -> tuple[Sandbox, EventBus, EventStore]:
    bus = EventBus()
    store = EventStore()
    return Sandbox(event_bus=bus, event_store=store), bus, store


@pytest.mark.asyncio
async def test_agent_runtime_sandboxed_timeout_triggers_cleanup() -> None:
    sandbox, bus, store = _build_sandbox()
    runtime = AgentRuntime(bus=bus, store=store, sandbox=sandbox)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click"]), delay=5.0)
    await runtime.start(agent)
    policy = SandboxPolicy(timeout_seconds=0.05)
    task = Task(name="t", capability="desktop.click")

    with pytest.raises(SandboxTimeoutError):
        await runtime.execute(agent.agent_id, task, policy=policy)
    # cleanup hook (agent.stop) ran on breach
    assert agent.stops == 1


@pytest.mark.asyncio
async def test_agent_runtime_sandboxed_success() -> None:
    sandbox, bus, store = _build_sandbox()
    runtime = AgentRuntime(bus=bus, store=store, sandbox=sandbox)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click"]))
    await runtime.start(agent)
    policy = SandboxPolicy(timeout_seconds=5.0)
    task = Task(name="t", capability="desktop.click")
    art = await runtime.execute(agent.agent_id, task, policy=policy)
    assert art.type == "desktop.click"
    assert agent.stops == 0  # no breach -> no cleanup


@pytest.mark.asyncio
async def test_agent_runtime_backward_compat_no_sandbox() -> None:
    # No sandbox passed -> behaves exactly as before (no timeout enforcement)
    runtime = AgentRuntime()
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click"]))
    await runtime.start(agent)
    task = Task(name="t", capability="desktop.click")
    art = await runtime.execute(agent.agent_id, task)
    assert art.type == "desktop.click"


@pytest.mark.asyncio
async def test_workflow_engine_sandboxed_step_timeout() -> None:
    sandbox, bus, store = _build_sandbox()
    runtime = AgentRuntime(bus=bus, store=store)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click", "desktop.screenshot"]), delay=5.0)
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    await runtime.start(agent)
    engine = WorkflowEngine(runtime, ex, bus, store, sandbox=sandbox)
    wf = Workflow(name="wf", context={"sandbox_policy": {"timeout_seconds": 0.05}}, steps=[
        WorkflowStep(id="s1", name="click", capability="desktop.click", compensation="c1"),
        WorkflowStep(id="c1", name="comp", capability="desktop.screenshot"),
    ])
    inst = await engine.start(wf)
    with pytest.raises(SandboxTimeoutError):
        await engine.execute_step(inst, wf, agent)
    # breach cleanup attempts compensation
    assert inst.status == WorkflowStatus.FAILED or inst.status == WorkflowStatus.COMPENSATING


@pytest.mark.asyncio
async def test_workflow_engine_backward_compat_no_sandbox() -> None:
    runtime = AgentRuntime()
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click", "desktop.screenshot"]))
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    await runtime.start(agent)
    engine = WorkflowEngine(runtime, ex, EventBus(), EventStore())
    wf = Workflow(name="wf", steps=[
        WorkflowStep(id="s1", name="shot", capability="desktop.screenshot"),
        WorkflowStep(id="s2", name="click", capability="desktop.click"),
    ])
    inst = await engine.start(wf)
    while inst.status == WorkflowStatus.RUNNING and inst.current_step_id is not None:
        await engine.execute_step(inst, wf, agent)
    assert inst.status == WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_sandbox_violation_event_on_workflow_breach() -> None:
    sandbox, bus, store = _build_sandbox()
    runtime = AgentRuntime(bus=bus, store=store)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.click", "desktop.screenshot"]), delay=5.0)
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    await runtime.start(agent)
    engine = WorkflowEngine(runtime, ex, bus, store, sandbox=sandbox)
    captured: list = []

    async def _cap(e):
        captured.append(e)

    bus.subscribe("sandbox.violation", _cap)
    wf = Workflow(name="wf", context={"sandbox_policy": {"timeout_seconds": 0.05}}, steps=[
        WorkflowStep(id="s1", name="click", capability="desktop.click", compensation="c1"),
        WorkflowStep(id="c1", name="comp", capability="desktop.screenshot"),
    ])
    inst = await engine.start(wf)
    with pytest.raises(SandboxTimeoutError):
        await engine.execute_step(inst, wf, agent)
    await asyncio.sleep(0.02)
    assert any(isinstance(e, SandboxViolationEvent) for e in captured)


@pytest.mark.asyncio
async def test_plugin_manifest_carries_sandbox_policy() -> None:
    """ADR-020: PluginManifest stores sandbox_policy; registry round-trips it."""
    from kernel.domain import PluginManifest
    from kernel.registry import PluginRegistry

    manifest = PluginManifest(
        name="desktop_control",
        version="2.3.0",
        capabilities=["hermes.desktop"],
        entrypoint="plugins.builtin.desktop_control:DesktopControlPlugin",
        sandbox_policy={
            "max_cpu_time_ms": 10000,
            "max_memory_mb": 256,
            "timeout_seconds": 10.0,
            "allow_network": False,
        },
    )
    # registry stores + returns the manifest unchanged (policy preserved)
    reg = PluginRegistry()
    reg.register_sync(manifest, object())
    got = await reg.get("desktop_control")
    assert got is not None
    stored_manifest = got[0]
    assert stored_manifest.sandbox_policy == manifest.sandbox_policy
    assert stored_manifest.sandbox_policy["timeout_seconds"] == 10.0
