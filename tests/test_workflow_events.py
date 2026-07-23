"""tests/test_workflow_events.py — Workflow* DomainEvent emission (ADR-019).

Verifies that every WorkflowEngine state transition publishes the correct
DomainEvent through the EventBus and appends it to the EventStore. Events are
pure data; we subscribe and capture them.
"""

from __future__ import annotations

import asyncio

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.bus import EventBus
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task, Workflow, WorkflowStatus, WorkflowStep
from kernel.events import EventStore
from kernel.workflow import WorkflowEngine


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)
        self._running = False

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(type=task.capability, content={"ok": True}, format="json")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running" if self._running else "stopped"}


def _capture(bus: EventBus) -> list:
    captured: list = []
    for etype in (
        "workflow.step_started",
        "workflow.step_completed",
        "workflow.step_failed",
        "workflow.step_awaiting_approval",
        "workflow.compensating",
    ):
        bus.subscribe(etype, lambda e, _t=etype: captured.append((_t, e)))
    return captured


@pytest.mark.asyncio
async def test_events_emitted_on_linear_flow() -> None:
    bus = EventBus()
    store = EventStore()
    runtime = AgentRuntime(bus=bus, store=store)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.screenshot", "desktop.click"]))
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    engine = WorkflowEngine(runtime, ex, bus, store)
    captured = _capture(bus)

    wf = Workflow(name="demo", steps=[
        WorkflowStep(id="s1", name="shot", capability="desktop.screenshot"),
        WorkflowStep(id="s2", name="click", capability="desktop.click"),
    ])
    inst = await engine.start(wf)
    while inst.status == WorkflowStatus.RUNNING and inst.current_step_id is not None:
        await engine.execute_step(inst, wf)
    await asyncio.sleep(0.02)  # let bus deliver

    types = [t for t, _ in captured]
    assert "workflow.step_started" in types
    assert "workflow.step_completed" in types
    assert types.count("workflow.step_completed") == 2
    # events also landed in the store
    assert store.count() >= 4


@pytest.mark.asyncio
async def test_event_emitted_on_approval_pause() -> None:
    bus = EventBus()
    store = EventStore()
    runtime = AgentRuntime(bus=bus, store=store)
    agent = FakeAgent(Agent(name="d", capabilities=["desktop.screenshot", "desktop.click"]))
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    engine = WorkflowEngine(runtime, ex, bus, store)
    captured = _capture(bus)

    wf = Workflow(name="approval", steps=[
        WorkflowStep(id="s1", name="shot", capability="desktop.screenshot", requires_approval=True),
    ])
    inst = await engine.start(wf)
    await engine.execute_step(inst, wf)
    await asyncio.sleep(0.02)
    assert any(t == "workflow.step_awaiting_approval" for t, _ in captured)
    assert inst.status == WorkflowStatus.PAUSED


@pytest.mark.asyncio
async def test_event_emitted_on_compensation() -> None:
    bus = EventBus()
    store = EventStore()
    runtime = AgentRuntime(bus=bus, store=store)

    class Flaky(BaseAgent):
        def __init__(self, e):
            super().__init__(e)
            self._r = False

        async def start(self):
            self._r = True
            return self.agent_id

        async def stop(self, a):
            self._r = False
            return True

        async def execute(self, a, t):  # type: ignore[override]
            if t.capability == "desktop.click":
                raise RuntimeError("boom")
            return Artifact(type=t.capability, content={}, format="json")

        async def status(self, a):
            return {}

    agent = Flaky(Agent(name="d", capabilities=["desktop.click", "desktop.screenshot"]))
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    engine = WorkflowEngine(runtime, ex, bus, store)
    captured = _capture(bus)
    wf = Workflow(name="comp", steps=[
        WorkflowStep(
            id="s1", name="click", capability="desktop.click",
            retry_policy={"max_attempts": 1, "backoff_seconds": 0.0, "exponential": False},
            compensation="c1",
        ),
        WorkflowStep(id="c1", name="comp", capability="desktop.screenshot"),
    ])
    inst = await engine.start(wf)
    await engine.execute_step(inst, wf)
    await asyncio.sleep(0.02)
    assert any(t == "workflow.compensating" for t, _ in captured)
    assert inst.status == WorkflowStatus.FAILED
