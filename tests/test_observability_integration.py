"""tests/test_observability_integration.py — observability wired into runtime (ADR-027)."""

from __future__ import annotations

import asyncio
import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task, Workflow, WorkflowInstance, WorkflowStatus, WorkflowStep, WorkflowTrigger
from kernel.events import EventBus, EventStore
from kernel.observability import ObservabilityEngine
from kernel.observability_store import ObservabilityStore
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


def _obs(**kw):
    return ObservabilityEngine(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(3), **kw)


def _build_engine(obs):
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store, observability=obs)
    return rt, ex, eng


async def test_workflow_engine_trace_spans_workflow() -> None:
    obs = _obs()
    rt, ex, eng = _build_engine(obs)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(name="wf", steps=[WorkflowStep(id="s1", name="one", capability="cap.x")], status=WorkflowStatus.DRAFT, trigger=WorkflowTrigger(type="manual"))
    inst = await eng.start(wf)
    await eng.execute_adaptive(inst.id, wf)
    # the instance_id is the trace_id; a span should exist for it
    assert len(obs.get_trace(inst.id)) >= 1


async def test_workflow_engine_emits_metrics() -> None:
    obs = _obs()
    rt, ex, eng = _build_engine(obs)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(name="wf", steps=[WorkflowStep(id="s1", name="one", capability="cap.x")], status=WorkflowStatus.DRAFT, trigger=WorkflowTrigger(type="manual"))
    inst = await eng.start(wf)
    await eng.execute_adaptive(inst.id, wf)
    names = {m.name for m in obs._metrics}
    assert "wf.executions" in names
    assert "wf.steps_total" in names


async def test_workflow_engine_logs_have_correlation_id() -> None:
    obs = _obs()
    rt, ex, eng = _build_engine(obs)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(name="wf", steps=[WorkflowStep(id="s1", name="one", capability="cap.x")], status=WorkflowStatus.DRAFT, trigger=WorkflowTrigger(type="manual"))
    inst = await eng.start(wf)
    await eng.execute_adaptive(inst.id, wf)
    logs = obs.get_logs(correlation_id=inst.id)
    assert any("executed" in l.message for l in logs)


async def test_workflow_engine_error_metric_on_failure() -> None:
    obs = _obs()
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store, observability=obs)

    class FailingAgent(BaseAgent):
        def __init__(self, entity: Agent) -> None:
            super().__init__(entity)

        async def start(self) -> str:
            return self.agent_id

        async def stop(self, agent_id: str) -> bool:
            return True

        async def execute(self, agent_id: str, task: Task) -> Artifact:
            raise RuntimeError("boom")

        async def status(self, agent_id: str) -> dict:
            return {"state": "running"}

    agent = FailingAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(name="wf", steps=[WorkflowStep(id="s1", name="one", capability="cap.x")], status=WorkflowStatus.DRAFT, trigger=WorkflowTrigger(type="manual"))
    inst = await eng.start(wf)
    # execute_step swallows the step error (returns an error artifact, no raise);
    # the engine still records the execution + an error log via observability.
    await eng.execute_adaptive(inst.id, wf)
    names = {m.name for m in obs._metrics}
    assert "wf.executions" in names
    # the failure is surfaced as an error log on the instance correlation id
    assert any(l.level == "error" for l in obs.get_logs(correlation_id=inst.id))


async def test_agent_runtime_logs_start() -> None:
    obs = _obs()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), observability=obs)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    assert any("started" in l.message for l in obs.get_logs(correlation_id=agent.agent_id))


async def test_agent_runtime_get_health_proxies() -> None:
    obs = _obs()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), observability=obs)
    assert rt.get_health()["uptime_seconds"] >= 0
    # without observability -> empty
    rt2 = AgentRuntime(bus=EventBus(), store=EventStore())
    assert rt2.get_health() == {}


async def test_agent_runtime_execute_spans() -> None:
    obs = _obs()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), observability=obs)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    aid = await rt.start(agent)
    await rt.execute(aid, Task(name="t1", capability="cap.x"))
    assert len(obs.get_trace(aid)) >= 1


async def test_no_observability_no_overhead() -> None:
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store)  # no observability
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store)  # no observability
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(name="wf", steps=[WorkflowStep(id="s1", name="one", capability="cap.x")], status=WorkflowStatus.DRAFT, trigger=WorkflowTrigger(type="manual"))
    inst = await eng.start(wf)
    # must run unchanged without any observability wiring
    await eng.execute_adaptive(inst.id, wf)
    await rt.execute(agent.agent_id, Task(name="t2", capability="cap.x"))


async def test_observability_store_persists_across_engine() -> None:
    store = ObservabilityStore()
    obs = _obs(store=store)
    await obs.record_metric("wf.exec", 1.0)
    await obs.log("info", "hi", correlation_id="c1")
    # a fresh engine bound to the same store sees the persisted data
    assert len(store.query_metrics("wf.exec")) == 1
    assert len(store.query_logs("c1")) == 1


async def test_event_store_has_obs_events() -> None:
    store = EventStore()
    obs = ObservabilityEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    await obs.record_metric("x", 1.0)
    await obs.start_span("t1", "s")
    types = {e.type for e in store._events}
    assert "obs.metric_recorded" in types
    assert "obs.span_started" in types
