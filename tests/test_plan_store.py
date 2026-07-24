"""tests/test_plan_store.py — PlanStore persistence (ADR-024)."""

from __future__ import annotations

import pytest
from kernel.domain import ExecutionOutcome, Plan, PlanStep, PlanStatus
from kernel.plan_store import PlanStore


def test_put_get_delete_plan_memory() -> None:
    s = PlanStore()
    plan = Plan(plan_id="p1", workflow_id="w1", status=PlanStatus.DRAFT, steps=[PlanStep(step_id="s1", capability="c")])
    s.put(plan)
    assert s.get("p1") is not None
    assert s.delete("p1") is True
    assert s.get("p1") is None
    assert s.delete("p1") is False


def test_put_get_outcome_memory() -> None:
    s = PlanStore()
    o = ExecutionOutcome(outcome_id="o1", plan_id="p1", step_id="s1", status="success", duration_ms=5)
    s.put_outcome(o)
    assert s.get_outcome("o1").status == "success"
    assert s.outcomes_for("p1")[0].outcome_id == "o1"


def test_sqlite_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "plans.db")
    s = PlanStore(db)
    plan = Plan(plan_id="p1", workflow_id="w1", status=PlanStatus.DRAFT, steps=[PlanStep(step_id="s1", capability="c")])
    o = ExecutionOutcome(outcome_id="o1", plan_id="p1", step_id="s1", status="success", duration_ms=5)
    s.put(plan)
    s.put_outcome(o)
    s2 = PlanStore(db)
    assert s2.get("p1") is not None
    assert s2.get_outcome("o1").status == "success"


def test_list_by_workflow_id() -> None:
    s = PlanStore()
    s.put(Plan(plan_id="p1", workflow_id="w1", status=PlanStatus.DRAFT, steps=[]))
    s.put(Plan(plan_id="p2", workflow_id="w1", status=PlanStatus.DRAFT, steps=[]))
    s.put(Plan(plan_id="p3", workflow_id="w2", status=PlanStatus.DRAFT, steps=[]))
    assert len(s.list_by_workflow("w1")) == 2


def test_sqlite_load_all_on_init(tmp_path) -> None:
    db = str(tmp_path / "plans.db")
    s1 = PlanStore(db)
    s1.put(Plan(plan_id="p1", workflow_id="w1", status=PlanStatus.DRAFT, steps=[PlanStep(step_id="s1", capability="c")]))
    s1.put_outcome(ExecutionOutcome(outcome_id="o1", plan_id="p1", step_id="s1", status="success", duration_ms=5))
    # Новый инстанс должен подгрузить из SQLite благодаря _load_all в __init__
    s2 = PlanStore(db)
    assert s2.get("p1") is not None
    assert s2.get("p1").plan_id == "p1"
    assert s2.get_outcome("o1") is not None
    assert s2.delete("p1") is True
    s3 = PlanStore(db)
    assert s3.get("p1") is None
