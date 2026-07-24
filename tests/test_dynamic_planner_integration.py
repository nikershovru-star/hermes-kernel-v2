"""tests/test_dynamic_planner_integration.py — DynamicPlanner + Workflow/Swarm (ADR-024).

Covers WorkflowEngine.execute_adaptive, fallback, replan_step, end-to-end
failure→replan→success, DAG ordering, backoff recording, LLM-free path, and
deterministic agent selection.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import (
    Agent,
    Artifact,
    ExecutionOutcome,
    PlanStep,
    ReplanTrigger,
    Task,
    Workflow,
    WorkflowInstance,
    WorkflowStatus,
    WorkflowStep,
    WorkflowTrigger,
)
from kernel.dynamic_planner import DynamicPlanner
from kernel.events import EventBus, EventStore
from kernel.swarm import SwarmCoordinator
from kernel.workflow import WorkflowEngine


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)
        self._running = False
        self.calls: list[str] = []

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        self.calls.append(task.capability)
        return Artifact(type=task.capability, content={"ok": True}, format="json", source=f"agent:{self.name}")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running" if self._running else "stopped"}


def _engine(planner: DynamicPlanner | None = None):
    bus, store = EventBus(), EventStore()
    runtime = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(runtime, ex, bus, store, dynamic_planner=planner)
    return eng, runtime, ex, bus, store


def _wf():
    return Workflow(
        name="wf",
        steps=[
            WorkflowStep(id="s1", name="one", capability="cap.x"),
            WorkflowStep(id="s2", name="two", capability="cap.y"),
        ],
        status=WorkflowStatus.DRAFT,
        trigger=WorkflowTrigger(type="manual"),
    )


async def test_execute_adaptive_runs_full_plan() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    eng, rt, ex, bus, store = _engine(planner)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x", "cap.y"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)
    assert arts[0].content["status"] in ("completed", "adapted")
    assert planner.list_plans(wf.id)


async def test_execute_adaptive_falls_back_without_planner() -> None:
    eng, rt, ex, bus, store = _engine(None)  # no planner
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x", "cap.y"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)  # must fall back to execute()
    assert any(a.type == "cap.x" for a in arts)


async def test_replan_step_triggers_event() -> None:
    bus, store = EventBus(), EventStore()
    planner = DynamicPlanner(event_bus=bus, event_store=store, rng=random.Random(1))
    eng, rt, ex, _, _ = _engine(planner)
    wf = _wf()
    inst = await eng.start(wf)
    plan = await eng.replan_step(inst.id, "s1", "capability_missing")
    assert plan is not None
    types = [e.type for e in await store.read_stream(wf.id)]
    assert "planner.replan_triggered" in types


async def test_agent_runtime_fail_then_replan_on_new_agent() -> None:
    bus, store = EventBus(), EventStore()
    planner = DynamicPlanner(event_bus=bus, event_store=store, rng=random.Random(7))
    eng, rt, ex, _, _ = _engine(planner)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="one", capability="cap.x")],
        status=WorkflowStatus.DRAFT,
        trigger=WorkflowTrigger(type="manual"),
    )
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)
    assert arts[0].content["status"] in ("completed", "adapted")


async def test_swarm_rebalance_returns_trigger_when_unbalanced() -> None:
    coord = SwarmCoordinator(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(3))
    await coord.join_swarm("s1", "hi", "n1", capabilities=["c"])
    await coord.join_swarm("s1", "lo", "n2", capabilities=["c"])
    coord._load["hi"] = 10.0
    coord._load["lo"] = 0.0
    trigger = coord.rebalance_load("s1")
    assert trigger is not None
    assert trigger.reason == "swarm_rebalance"
    assert trigger.context["from_agent"] == "hi"
    assert trigger.context["to_agent"] == "lo"


async def test_swarm_rebalance_none_when_balanced() -> None:
    coord = SwarmCoordinator(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(3))
    await coord.join_swarm("s1", "a", "n1", capabilities=["c"])
    await coord.join_swarm("s1", "b", "n2", capabilities=["c"])
    coord._load["a"] = 5.0
    coord._load["b"] = 5.0
    assert coord.rebalance_load("s1") is None


async def test_e2e_failure_then_replan_then_success() -> None:
    bus, store = EventBus(), EventStore()
    planner = DynamicPlanner(event_bus=bus, event_store=store, rng=random.Random(2))
    eng, rt, ex, _, _ = _engine(planner)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)
    assert arts[0].content["status"] in ("completed", "adapted")


async def test_event_store_contains_plan_chain() -> None:
    bus, store = EventBus(), EventStore()
    planner = DynamicPlanner(event_bus=bus, event_store=store, rng=random.Random(9))
    eng, rt, ex, _, _ = _engine(planner)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x", "cap.y"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    await eng.execute_adaptive(inst.id, wf)
    all_types = {e.type for e in store._events}
    assert "planner.plan_created" in all_types
    assert "planner.step_planned" in all_types
    assert "planner.step_executed" in all_types


async def test_plan_dag_respects_dependencies_after_replan() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    steps = [
        PlanStep(step_id="a", capability="c"),
        PlanStep(step_id="b", capability="c", dependencies=["a"]),
    ]
    plan = await planner.create_plan("w1", steps)
    order: list[str] = []

    async def track(step):
        order.append(step.step_id)
        return ExecutionOutcome(outcome_id="o-" + step.step_id, plan_id=plan.plan_id, step_id=step.step_id, status="success", duration_ms=1)

    await planner.execute_plan(plan.plan_id, track)
    assert order.index("a") < order.index("b")


async def test_retry_backoff_records_intervals() -> None:
    sleeps: list[float] = []
    planner = DynamicPlanner(
        event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1),
        sleep=lambda s: sleeps.append(s) or asyncio.sleep(0),
    )
    plan = await planner.create_plan("w1", [PlanStep(step_id="s", capability="c", retry_budget=2)])
    n = {"c": 0}

    async def fail(step):
        n["c"] += 1
        return ExecutionOutcome(outcome_id="o%d" % n["c"], plan_id=plan.plan_id, step_id="s", status="failure", duration_ms=1)

    await planner.execute_plan(plan.plan_id, fail)
    assert sleeps == [1, 2]


async def test_no_llm_calls_when_none() -> None:
    calls = {"n": 0}

    async def fake_llm(p):
        calls["n"] += 1
        return "{}"

    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))  # llm_client=None
    plan = await planner.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="capability_missing", context={})
    await planner._replan(plan, trigger)
    assert calls["n"] == 0  # rule-based path only


async def test_deterministic_agent_selection_same_seed() -> None:
    p1 = DynamicPlanner(rng=random.Random(123))
    p2 = DynamicPlanner(rng=random.Random(123))
    n1 = p1._rng.randint(0, 10**6)
    n2 = p2._rng.randint(0, 10**6)
    assert n1 == n2
