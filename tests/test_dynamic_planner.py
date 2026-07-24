"""tests/test_dynamic_planner.py — DynamicPlanner (ADR-024).

Deterministic: injectable sleep (instant), seeded RNG, in-memory bus/store,
mock LLM client. No real network / asyncio.sleep delays.
"""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from kernel.domain import (
    ExecutionOutcome,
    Plan,
    PlanStatus,
    PlanStep,
    ReplanTrigger,
    RiskLevel,
)
from kernel.dynamic_planner import DynamicPlanner
from kernel.events import EventBus, EventStore
from kernel.plan_store import PlanStore


def _planner(**kw):
    base = dict(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(42))
    base.update(kw)
    return DynamicPlanner(**base)


async def _ok(step):
    return ExecutionOutcome(
        outcome_id="o-" + step.step_id, plan_id="p", step_id=step.step_id,
        status="success", duration_ms=5,
    )


async def test_create_plan_emits_events_and_stores() -> None:
    store = EventStore()
    p = _planner(event_store=store)
    plan = await p.create_plan("w1", [PlanStep(step_id="s1", capability="cap.x")])
    assert plan.plan_id
    assert p.get_plan(plan.plan_id) is plan
    types = [e.type for e in await store.read_stream(plan.plan_id)]
    assert "planner.plan_created" in types
    assert "planner.step_planned" in types


async def test_get_plan_and_list_plans() -> None:
    p = _planner()
    a = await p.create_plan("w1", [PlanStep(step_id="s1", capability="c")])
    b = await p.create_plan("w1", [PlanStep(step_id="s1", capability="c")])
    c = await p.create_plan("w2", [PlanStep(step_id="s1", capability="c")])
    assert p.get_plan(a.plan_id) is not None
    assert len(p.list_plans("w1")) == 2
    assert len(p.list_plans("w2")) == 1


async def test_execute_plan_linear_success() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    res = await p.execute_plan(plan.plan_id, _ok)
    assert res.status == PlanStatus.COMPLETED


async def test_execute_plan_dag_dependency_order() -> None:
    p = _planner()
    steps = [
        PlanStep(step_id="a", capability="c"),
        PlanStep(step_id="b", capability="c", dependencies=["a"]),
    ]
    plan = await p.create_plan("w1", steps)
    order: list[str] = []

    async def track(step):
        order.append(step.step_id)
        return ExecutionOutcome(outcome_id="o-" + step.step_id, plan_id=plan.plan_id, step_id=step.step_id, status="success", duration_ms=1)

    await p.execute_plan(plan.plan_id, track)
    assert order.index("a") < order.index("b")


async def test_retry_on_failure_with_backoff() -> None:
    p = _planner()
    sleeps: list[float] = []
    p._sleep = lambda s: sleeps.append(s) or asyncio.sleep(0)
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", retry_budget=2)])
    calls = {"n": 0}

    async def flaky(step):
        calls["n"] += 1
        if calls["n"] < 3:
            return ExecutionOutcome(outcome_id="o%d" % calls["n"], plan_id=plan.plan_id, step_id="s", status="failure", duration_ms=2)
        return ExecutionOutcome(outcome_id="o%d" % calls["n"], plan_id=plan.plan_id, step_id="s", status="success", duration_ms=2)

    res = await p.execute_plan(plan.plan_id, flaky)
    assert res.status == PlanStatus.COMPLETED
    assert calls["n"] == 3
    assert sleeps == [1, 2]  # exponential backoff 2^(retry-1)


async def test_retry_exhaustion_triggers_replan() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", retry_budget=1)])
    n = {"c": 0}

    async def always_fail(step):
        n["c"] += 1
        return ExecutionOutcome(outcome_id="o%d" % n["c"], plan_id=plan.plan_id, step_id="s", status="failure", duration_ms=1)

    res = await p.execute_plan(plan.plan_id, always_fail)
    assert len(p._replans) == 1
    assert p._replans[list(p._replans)[0]].reason == "step_failed"


async def test_replan_capability_missing_unassigns() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", agent_id="a1")])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="capability_missing", context={})
    new = await p._replan(plan, trigger)
    assert new.steps[0].agent_id is None
    assert new.steps[0].risk == RiskLevel.HIGH
    assert new.version == plan.version + 1


async def test_replan_agent_unhealthy_reassigns() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [
        PlanStep(step_id="s", capability="c", agent_id="a1"),
        PlanStep(step_id="s2", capability="c2", agent_id="a2"),
    ])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="agent_unhealthy", context={"step_id": "s"})
    new = await p._replan(plan, trigger)
    assert new.steps[0].agent_id in ("a1", "a2")
    assert new.steps[0].agent_id != "a1" or new.steps[0].agent_id is None


async def test_replan_step_failed_splits_substeps() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", estimated_duration_ms=1000)])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="step_failed", context={"step_id": "s"})
    new = await p._replan(plan, trigger)
    ids = {s.step_id for s in new.steps}
    assert "s-a" in ids and "s-b" in ids
    for s in new.steps:
        if s.step_id in ("s-a", "s-b"):
            assert s.estimated_duration_ms == 500


async def test_replan_risk_escalation_bumps_risk_and_budget() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", risk=RiskLevel.LOW, retry_budget=3)])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="risk_escalation", context={"step_id": "s"})
    new = await p._replan(plan, trigger)
    assert new.steps[0].risk == RiskLevel.MEDIUM
    assert new.steps[0].retry_budget == 4


async def test_replan_swarm_rebalance_reassigns() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [
        PlanStep(step_id="s", capability="c", agent_id="hi-agent"),
        PlanStep(step_id="s2", capability="c2", agent_id="lo-agent"),
    ])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="swarm_rebalance", context={"from_agent": "hi-agent", "to_agent": "lo-agent"})
    new = await p._replan(plan, trigger)
    assert new.steps[0].agent_id == "lo-agent"


async def test_llm_replan_valid_json() -> None:
    async def mock_llm(prompt: str) -> str:
        return '{"steps": [{"step_id": "s", "capability": "c", "agent_id": null, "dependencies": [], "estimated_duration_ms": 500, "risk": "low", "retry_budget": 3}]}'

    p = _planner(llm_client=mock_llm)
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="step_failed", context={"step_id": "s"})
    new = await p._replan(plan, trigger)
    assert new.steps[0].estimated_duration_ms == 500


async def test_llm_replan_falls_back_to_rules_on_garbage() -> None:
    async def mock_llm(prompt: str) -> str:
        return "not json at all {{{"

    p = _planner(llm_client=mock_llm)
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c", risk=RiskLevel.LOW)])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="risk_escalation", context={"step_id": "s"})
    new = await p._replan(plan, trigger)  # must not raise; rule-based path
    assert new.steps[0].risk == RiskLevel.MEDIUM


async def test_risk_assess_escalates_high_after_many_failures() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    hist = [
        ExecutionOutcome(outcome_id="h%d" % i, plan_id=plan.plan_id, step_id="s", status="failure", duration_ms=1)
        for i in range(4)
    ]
    res = await p.risk_assess(plan, hist)
    assert res.steps[0].risk == RiskLevel.HIGH


async def test_risk_assess_critical_when_agent_unhealthy() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    hist = [ExecutionOutcome(outcome_id="h1", plan_id=plan.plan_id, step_id="s", status="failure", duration_ms=1, error_type="agent_unhealthy")]
    res = await p.risk_assess(plan, hist)
    assert res.steps[0].risk == RiskLevel.CRITICAL


async def test_plan_version_increments_on_adapt() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="capability_missing", context={})
    new = await p._replan(plan, trigger)
    assert new.version == plan.version + 1


async def test_persistence_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "plans.db")
    store = PlanStore(db)
    p = _planner(store=store)
    plan = await p.create_plan("w1", [PlanStep(step_id="s", capability="c")])
    store2 = PlanStore(db)
    loaded = store2.get(plan.plan_id)
    assert loaded is not None


async def test_llm_replan_success_path_covers_json_parse() -> None:
    async def mock_llm(prompt: str) -> str:
        return json.dumps({
            "steps": [
                {"step_id": "s1-new", "capability": "cap.x", "agent_id": "a2", "dependencies": [], "estimated_duration_ms": 500, "risk": "low", "retry_budget": 2}
            ]
        })
    p = _planner(llm_client=mock_llm)
    plan = await p.create_plan("w1", [PlanStep(step_id="s1", capability="cap.x", agent_id="a1")])
    trigger = ReplanTrigger(trigger_id="t1", plan_id=plan.plan_id, reason="step_failed", context={"step_id": "s1"})
    adapted = await p._replan(plan, trigger)
    assert adapted.version == 2
    assert adapted.steps[0].step_id == "s1-new"
    assert adapted.steps[0].agent_id == "a2"


async def test_execute_plan_recovery_after_replan() -> None:
    p = _planner()
    plan = await p.create_plan("w1", [PlanStep(step_id="s1", capability="cap.x", agent_id="a1", retry_budget=0)])
    fail_count = 0

    async def flaky(step):
        nonlocal fail_count
        fail_count += 1
        if fail_count == 1:
            return ExecutionOutcome(outcome_id="o1", plan_id=plan.plan_id, step_id=step.step_id, status="failure", duration_ms=1)
        return ExecutionOutcome(outcome_id="o2", plan_id=plan.plan_id, step_id=step.step_id, status="success", duration_ms=1)

    res = await p.execute_plan(plan.plan_id, flaky)
    assert res.status == PlanStatus.ADAPTED
    assert fail_count == 3  # 1 original fail + 2 split substep attempts (s1-a, s1-b)
