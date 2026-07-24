"""kernel/dynamic_planner.py — Dynamic Planner (ADR-024).

Adaptive replanning + risk-aware execution for workflow plans.

Responsibilities:
- Build a ``Plan`` (DAG of ``PlanStep``) for a workflow.
- Execute it respecting step dependencies (topological order).
- Retry failed steps with exponential backoff (injectable ``sleep``).
- When retries are exhausted, trigger adaptive replanning:
  - rule-based (deterministic, no LLM) for all 5 trigger reasons, or
  - LLM-based (injectable ``llm_client``) when provided, with safe
    fallback to the rule-based path on garbage/timeout.
- Risk assessment that escalates ``RiskLevel`` from an execution history.
- Optional persistence (``PlanStore``) and optional swarm coordination
  (``SwarmCoordinator`` for capability discovery / load rebalance).

AXIS CONTRACT: depends only on ``kernel.domain`` + ``kernel.events``.
``kernel.swarm`` is imported lazily (only when a ``SwarmCoordinator`` is
actually wired in). Never imports ``plugins/`` or ``mcp/``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.domain import (
    ExecutionOutcome,
    Plan,
    PlanStatus,
    PlanStep,
    ReplanTrigger,
    RiskLevel,
)
from kernel.events import (
    EventBus,
    EventStore,
    PlanAdapted,
    PlanCreated,
    ReplanTriggered,
    RiskEscalated,
    StepExecuted,
    StepPlanned,
)

logger = logging.getLogger("hermes.kernel.dynamic_planner")

SleepFn = Callable[[float], Awaitable[None]]
LLMClient = Callable[[str], Awaitable[str]]

_RISK_ORDER: list[RiskLevel] = [
    RiskLevel.LOW,
    RiskLevel.MEDIUM,
    RiskLevel.HIGH,
    RiskLevel.CRITICAL,
]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _bump_risk(risk: RiskLevel, levels: int = 1) -> RiskLevel:
    idx = _RISK_ORDER.index(risk)
    return _RISK_ORDER[min(idx + levels, len(_RISK_ORDER) - 1)]


class DynamicPlanner:
    """Adaptive, risk-aware plan executor + replanner (ADR-024)."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        swarm_coordinator: Any | None = None,  # SwarmCoordinator (lazy import)
        llm_client: LLMClient | None = None,
        sleep: SleepFn | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] | None = None,
        store: Any | None = None,  # PlanStore (optional persistence)
    ) -> None:
        self._bus = event_bus
        self._store = event_store
        self._swarm = swarm_coordinator
        self._llm = llm_client
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._clock = clock or time.monotonic
        self._persist = store
        self._plans: dict[str, Plan] = {}
        self._outcomes: dict[str, ExecutionOutcome] = {}
        self._replans: dict[str, ReplanTrigger] = {}

    # -- plan lifecycle --------------------------------------------------- #
    async def create_plan(self, workflow_id: str, steps: list[PlanStep]) -> Plan:
        plan_id = uuid.uuid4().hex
        plan = Plan(
            plan_id=plan_id,
            workflow_id=workflow_id,
            status=PlanStatus.DRAFT,
            steps=list(steps),
        )
        self._plans[plan_id] = plan
        self._persist_plan(plan)
        await self._emit(PlanCreated(plan_id, workflow_id, len(steps), plan.version))
        for s in plan.steps:
            await self._emit(
                StepPlanned(plan_id, s.step_id, s.capability, s.agent_id, s.risk.value)
            )
        return plan

    def get_plan(self, plan_id: str) -> Plan | None:
        if plan_id in self._plans:
            return self._plans[plan_id]
        if self._persist is not None:
            return self._persist.get(plan_id)
        return None

    def list_plans(self, workflow_id: str) -> list[Plan]:
        plans = [p for p in self._plans.values() if p.workflow_id == workflow_id]
        if self._persist is not None:
            plans += self._persist.list_by_workflow(workflow_id)
        # de-dup by plan_id (in-memory wins)
        seen: dict[str, Plan] = {p.plan_id: p for p in plans}
        return list(seen.values())

    # -- execution -------------------------------------------------------- #
    async def execute_plan(
        self,
        plan_id: str,
        executor: Callable[[PlanStep], Awaitable[ExecutionOutcome]],
    ) -> Plan:
        plan = self.get_plan(plan_id)
        if plan is None:
            raise KeyError(f"plan '{plan_id}' not found")
        plan.status = PlanStatus.ACTIVE
        self._persist_plan(plan)
        ordered = self._toposort(plan)
        for step in ordered:
            await self._execute_step_with_retry(plan, step, executor)
        # settle final status
        if all(self._last_outcome(step.step_id).status == "success" for step in plan.steps):
            plan.status = PlanStatus.COMPLETED
        else:
            plan.status = PlanStatus.ADAPTED
        plan.updated_at = _now()
        self._persist_plan(plan)
        return plan

    async def _execute_step_with_retry(
        self,
        plan: Plan,
        step: PlanStep,
        executor: Callable[[PlanStep], Awaitable[ExecutionOutcome]],
    ) -> ExecutionOutcome:
        budget = step.retry_budget
        retry = 0
        while True:
            outcome = await executor(step)
            outcome.retry_count = retry
            self._record_outcome(outcome)
            dur = outcome.duration_ms
            if outcome.status == "success":
                await self._emit(
                    StepExecuted(plan.plan_id, step.step_id, "success", dur, retry)
                )
                return outcome
            # failure / timeout / cancelled -> maybe retry
            if budget > 0:
                budget -= 1
                retry += 1
                backoff = 2 ** (retry - 1)  # exponential: 1,2,4,...
                await self._emit(
                    StepExecuted(plan.plan_id, step.step_id, outcome.status, dur, retry)
                )
                await self._sleep(backoff)
                continue
            # exhausted -> trigger replan
            trigger = ReplanTrigger(
                trigger_id=uuid.uuid4().hex,
                plan_id=plan.plan_id,
                reason="step_failed",
                context={"step_id": step.step_id, "status": outcome.status},
            )
            self._replans[trigger.trigger_id] = trigger
            await self._emit(
                ReplanTriggered(
                    trigger.trigger_id, plan.plan_id, "step_failed", step.step_id
                )
            )
            new_plan = await self._replan(plan, trigger)
            # continue executing the adapted plan from the failed step
            plan.status = PlanStatus.ADAPTED
            # re-run execution over the adapted steps (idempotent: success short-circuits)
            adapted = self.get_plan(new_plan.plan_id)
            for s in self._toposort(adapted):
                if self._already_succeeded(adapted, s):
                    continue
                res = await executor(s)
                res.retry_count = 0
                self._record_outcome(res)
                await self._emit(
                    StepExecuted(adapted.plan_id, s.step_id, res.status, res.duration_ms, 0)
                )
                if res.status != "success":
                    # give up gracefully: leave step as-is
                    continue
            return self._last_outcome(step.step_id) or outcome

    def _already_succeeded(self, plan: Plan, step: PlanStep) -> bool:
        for o in self._outcomes.values():
            if o.plan_id == plan.plan_id and o.step_id == step.step_id and o.status == "success":
                return True
        return False

    def _last_outcome(self, step_id: str) -> ExecutionOutcome | None:
        last: ExecutionOutcome | None = None
        for o in self._outcomes.values():
            if o.step_id == step_id:
                if last is None or o.retry_count >= last.retry_count:
                    last = o
        return last

    # -- adaptive replanning --------------------------------------------- #
    async def _replan(self, plan: Plan, trigger: ReplanTrigger) -> Plan:
        old_version = plan.version
        new_steps: list[PlanStep] = list(plan.steps)
        changes: list[str] = []
        if self._llm is not None:
            try:
                new_steps = await self._replan_via_llm(plan, trigger, new_steps)
                changes.append("llm_replan")
            except Exception as exc:  # noqa: BLE001 - never crash on LLM failure
                logger.warning("LLM replan failed, falling back to rules: %s", exc)
                new_steps = self._replan_rule_based(plan, trigger, new_steps, changes)
        else:
            new_steps = self._replan_rule_based(plan, trigger, new_steps, changes)

        new_plan = Plan(
            plan_id=uuid.uuid4().hex,
            workflow_id=plan.workflow_id,
            status=PlanStatus.ADAPTED,
            steps=new_steps,
            version=old_version + 1,
        )
        self._plans[new_plan.plan_id] = new_plan
        self._persist_plan(new_plan)
        await self._emit(
            PlanAdapted(
                new_plan.plan_id, old_version, new_plan.version, "; ".join(changes)
            )
        )
        return new_plan

    def _replan_rule_based(
        self, plan: Plan, trigger: ReplanTrigger, steps: list[PlanStep], changes: list[str]
    ) -> list[PlanStep]:
        reason = trigger.reason
        if reason == "capability_missing":
            for s in steps:
                s.agent_id = None  # unassigned pool
                if s.risk != RiskLevel.HIGH:
                    changes.append(f"{s.step_id}:mark_high")
                s.risk = RiskLevel.HIGH
            changes.append("capability_missing:unassign")
        elif reason == "agent_unhealthy":
            failed = trigger.context.get("step_id")
            failed_agent = self._step_by_id(plan, failed).agent_id if self._step_by_id(plan, failed) else None
            cand = self._next_eligible_agent(plan, exclude=failed_agent)
            for s in steps:
                if s.step_id == failed or s.agent_id is None:
                    s.agent_id = cand
            changes.append(f"agent_unhealthy:reassign->{cand}")
        elif reason == "step_failed":
            failed = trigger.context.get("step_id")
            new_list: list[PlanStep] = []
            for s in steps:
                if s.step_id == failed:
                    sub_a = PlanStep(
                        step_id=f"{s.step_id}-a",
                        capability=s.capability,
                        agent_id=s.agent_id,
                        dependencies=list(s.dependencies),
                        estimated_duration_ms=max(1, s.estimated_duration_ms // 2),
                        risk=s.risk,
                        retry_budget=s.retry_budget,
                    )
                    sub_b = PlanStep(
                        step_id=f"{s.step_id}-b",
                        capability=s.capability,
                        agent_id=s.agent_id,
                        dependencies=list(s.dependencies) + [sub_a.step_id],
                        estimated_duration_ms=max(1, s.estimated_duration_ms // 2),
                        risk=s.risk,
                        retry_budget=s.retry_budget,
                    )
                    new_list.append(sub_a)
                    new_list.append(sub_b)
                    changes.append(f"step_failed:split->{s.step_id}-a/-b")
                else:
                    new_list.append(s)
            steps = new_list
        elif reason == "risk_escalation":
            failed = trigger.context.get("step_id")
            for s in steps:
                if s.step_id == failed:
                    s.risk = _bump_risk(s.risk, 1)
                    s.retry_budget += 1
                    changes.append(f"risk_escalation:{failed}->{s.risk.value}")
        elif reason == "swarm_rebalance":
            from_id = trigger.context.get("from_agent")
            to_id = trigger.context.get("to_agent")
            if from_id is not None and to_id is not None:
                for s in steps:
                    if s.agent_id == from_id:
                        s.agent_id = to_id
                changes.append(f"swarm_rebalance:{from_id}->{to_id}")
        return steps

    async def _replan_via_llm(
        self, plan: Plan, trigger: ReplanTrigger, fallback_steps: list[PlanStep]
    ) -> list[PlanStep]:
        prompt = json.dumps(
            {
                "plan": plan.model_dump(mode="json"),
                "trigger": trigger.model_dump(mode="json"),
                "instruction": "Return JSON: {\"steps\": [{\"step_id\":..,\"capability\":..,\"agent_id\":..,\"dependencies\":[..],\"estimated_duration_ms\":..,\"risk\":..,\"retry_budget\":..}]}",
            }
        )
        raw = await self._llm(prompt)
        data = json.loads(raw)  # may raise -> caller falls back to rules
        parsed: list[PlanStep] = []
        for d in data.get("steps", []):
            parsed.append(
                PlanStep(
                    step_id=d["step_id"],
                    capability=d["capability"],
                    agent_id=d.get("agent_id"),
                    dependencies=d.get("dependencies", []),
                    estimated_duration_ms=int(d.get("estimated_duration_ms", 1000)),
                    risk=RiskLevel(d.get("risk", "low")),
                    retry_budget=int(d.get("retry_budget", 3)),
                )
            )
        if not parsed:
            raise ValueError("LLM returned empty step list")
        return parsed

    def _next_eligible_agent(self, plan: Plan, exclude: str | None = None) -> str | None:
        # prefer swarm round-robin if available; else unassigned
        if self._swarm is not None:
            try:
                from kernel.swarm import SwarmCoordinator  # lazy import (axis-safe)

                if isinstance(self._swarm, SwarmCoordinator):
                    members = list(self._swarm.get_swarm(plan.plan_id).members.values())
                    healthy = [
                        m.agent_id
                        for m in members
                        if m.health in ("healthy", "suspected") and m.agent_id != exclude
                    ]
                    if healthy:
                        return self._rng.choice(healthy)
            except Exception:  # noqa: BLE001
                pass
        # fall back: any distinct agent mentioned in the plan
        agents = {s.agent_id for s in plan.steps if s.agent_id and s.agent_id != exclude}
        if agents:
            return self._rng.choice(list(agents))
        return None

    # -- risk assessment -------------------------------------------------- #
    async def risk_assess(self, plan: Plan, history: list[ExecutionOutcome]) -> Plan:
        failures_by_cap: dict[str, int] = {}
        unhealthy_steps: set[str] = set()
        for o in history:
            if o.status in ("failure", "timeout"):
                step = self._step_by_id(plan, o.step_id)
                if step is not None:
                    failures_by_cap[step.capability] = failures_by_cap.get(step.capability, 0) + 1
            if o.error_type == "agent_unhealthy":
                unhealthy_steps.add(o.step_id)
        changed = False
        for step in plan.steps:
            old = step.risk
            if step.step_id in unhealthy_steps:
                if step.risk != RiskLevel.CRITICAL:
                    step.risk = RiskLevel.CRITICAL
                    changed = True
            elif failures_by_cap.get(step.capability, 0) > 2:
                if step.risk != RiskLevel.HIGH:
                    step.risk = RiskLevel.HIGH
                    changed = True
            if step.risk != old:
                await self._emit(
                    RiskEscalated(
                        plan.plan_id, step.step_id, old.value, step.risk.value, "risk_assessment"
                    )
                )
                changed = True
        if changed:
            plan.updated_at = _now()
            self._persist_plan(plan)
        return plan

    # -- helpers ---------------------------------------------------------- #
    def _toposort(self, plan: Plan) -> list[PlanStep]:
        by_id = {s.step_id: s for s in plan.steps}
        visited: set[str] = set()
        order: list[PlanStep] = []

        def visit(sid: str, stack: set[str]) -> None:
            if sid in visited:
                return
            if sid in stack:
                raise ValueError(f"dependency cycle detected at step '{sid}'")
            step = by_id.get(sid)
            if step is None:
                return  # dependency on a non-existent step -> ignore
            stack.add(sid)
            for dep in step.dependencies:
                visit(dep, stack)
            stack.discard(sid)
            visited.add(sid)
            order.append(step)

        for s in plan.steps:
            visit(s.step_id, set())
        return order

    def _step_by_id(self, plan: Plan, step_id: str) -> PlanStep | None:
        for s in plan.steps:
            if s.step_id == step_id:
                return s
        return None

    def _record_outcome(self, outcome: ExecutionOutcome) -> None:
        self._outcomes[outcome.outcome_id] = outcome
        if self._persist is not None:
            self._persist.put_outcome(outcome)

    def _persist_plan(self, plan: Plan) -> None:
        if self._persist is not None:
            self._persist.put(plan)

    async def _emit(self, event: Any) -> None:
        if self._store is not None:
            try:
                await self._store.append(event)
            except Exception:  # noqa: BLE001 - event persistence must never break execution
                pass
        if self._bus is not None:
            self._bus.publish(event)


__all__ = ["DynamicPlanner", "SleepFn", "LLMClient"]
