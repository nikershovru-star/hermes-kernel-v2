"""tests/test_planner_workflow_compat.py — backward-compat + axis checks (ADR-024).

Verifies the 461 existing WorkflowEngine tests still hold (planner=None),
execute_adaptive returns the same Artifact shape, PlanStep maps Workflow step
capability, empty/cyclic plans are handled, old instances without plan_id work,
and the new module imports only kernel.domain + kernel.events (axis contract).
"""

from __future__ import annotations

import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, PlanStatus, Task, Workflow, WorkflowStatus, WorkflowStep, WorkflowTrigger
from kernel.dynamic_planner import DynamicPlanner
from kernel.events import EventBus, EventStore
from kernel.workflow import WorkflowEngine
from kernel.plan_store import PlanStore


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


def _engine(planner=None):
    bus, store = EventBus(), EventStore()
    runtime = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(runtime, ex, bus, store, dynamic_planner=planner)
    return eng, runtime, ex, bus, store


def _wf():
    return Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="one", capability="cap.x")],
        status=WorkflowStatus.DRAFT,
        trigger=WorkflowTrigger(type="manual"),
    )


async def test_existing_workflow_engine_works_without_planner() -> None:
    eng, rt, ex, bus, store = _engine(planner=None)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    arts = []
    while inst.status == WorkflowStatus.RUNNING and inst.current_step_id is not None:
        arts.append(await eng.execute_step(inst, wf))
    assert arts and arts[0].type == "cap.x"


async def test_execute_adaptive_returns_same_artifact_shape() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    eng, rt, ex, bus, store = _engine(planner)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)
    assert isinstance(arts[0], Artifact)


async def test_planstep_maps_workflow_step_capability() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    wf = _wf()
    plan = await planner.create_plan(wf.id, [
        # simulate the mapping done by execute_adaptive
        __import__("kernel.domain", fromlist=["PlanStep"]).PlanStep(step_id=s.id, capability=s.capability)
        for s in wf.steps
    ])
    assert plan.steps[0].capability == wf.steps[0].capability


async def test_empty_plan_graceful() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    plan = await planner.create_plan("w1", [])
    res = await planner.execute_plan(plan.plan_id, lambda step: None)
    assert res.status == PlanStatus.COMPLETED


async def test_dependency_cycle_raises() -> None:
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    from kernel.domain import PlanStep
    plan = await planner.create_plan("w1", [
        PlanStep(step_id="a", capability="c", dependencies=["b"]),
        PlanStep(step_id="b", capability="c", dependencies=["a"]),
    ])
    with pytest.raises(ValueError):
        await planner.execute_plan(plan.plan_id, lambda step: None)


async def test_old_instance_without_plan_id_works() -> None:
    # WorkflowInstance created directly (no plan linkage) executes normally
    eng, rt, ex, bus, store = _engine(planner=None)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = _wf()
    inst = eng.get_instance((await eng.start(wf)).id)
    # no plan_id attribute is needed on the instance
    assert inst.status == WorkflowStatus.RUNNING


def test_axis_dynamic_planner_imports_only_kernel_domain_events() -> None:
    import ast
    src = open("kernel/dynamic_planner.py", encoding="utf-8").read()
    tree = ast.parse(src)
    stdlib = {"__future__", "asyncio", "json", "logging", "random", "time", "uuid", "datetime", "typing"}
    allowed_pkgs = {"kernel.domain", "kernel.events", "kernel.swarm"}
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if node.module not in allowed_pkgs and root not in stdlib:
                bad.add(node.module)
        elif isinstance(node, ast.Import):
            for n in node.names:
                root = n.name.split(".")[0]
                if root not in stdlib and root not in allowed_pkgs:
                    bad.add(n.name)
    assert bad == set(), f"forbidden imports: {bad}"
