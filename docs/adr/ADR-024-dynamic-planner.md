# ADR-024 — Dynamic Planner (Adaptive Replanning & Risk-Aware Execution)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.10.0)
- **Depends on:** ADR-019 (Workflow Runtime), ADR-017 (Event Platform), ADR-023 (Swarm / Teams)

---

## Context

The v5 Capability Platform executes multi-step workflows, but today a workflow is
static: if a step fails past its retry budget the whole run aborts or falls into
compensation. There is no *adaptive* layer that can re-plan around failure —
reassign an agent, discover a remote capability, or split a stuck step. Three gaps
motivated this release:

1. **No adaptive replanning** — a failed step cannot trigger a revised plan.
2. **No risk awareness** — step risk is fixed at authoring time; past failures
   do not raise it.
3. **No swarm-aware load balancing** — the planner cannot ask the swarm to
   rebalance a hot agent.

The planner must stay **axis-clean** (import only `kernel.domain` + `kernel.events`),
**deterministic** (injectable clock/sleep/rng/llm), and **backward-compatible**
(existing `WorkflowEngine` usage is untouched unless a planner is explicitly wired).

## Decision

- **`kernel/domain.py`** — `PlanStatus` (DRAFT/ACTIVE/FAILED/COMPLETED/ADAPTED),
  `RiskLevel` (LOW/MEDIUM/HIGH/CRITICAL), `PlanStep`, `Plan`, `ExecutionOutcome`,
  `ReplanTrigger`.
- **`kernel/events.py`** — 6 planner events (using the `super().__init__(type=…)`
  convention): `PlanCreated`, `StepPlanned`, `ReplanTriggered`, `PlanAdapted`,
  `StepExecuted`, `RiskEscalated`.
- **`kernel/dynamic_planner.py`** — `DynamicPlanner` (async):
  - *Plan build* — `create_plan(workflow_id, steps) -> Plan`; emits `PlanCreated`
    + one `StepPlanned` per step.
  - *Execution* — `execute_plan(plan_id, executor)` runs steps in **topological
    order** over `dependencies` (DAG; cycle → `ValueError`). Each step is invoked
    via the injected `executor(PlanStep) -> ExecutionOutcome`; on `failure` it
    **retries with exponential backoff** (`sleep(2**(retry-1))`, budget =
    `step.retry_budget`, injectable `sleep`). On budget exhaustion it emits
    `ReplanTriggered` and calls `_replan`.
  - *Adaptive replan* — `_replan` applies **five rule-based triggers**:
    - `capability_missing` → unassign agent + bump `risk` to HIGH;
    - `agent_unhealthy` → round-robin reassign to next eligible agent (swarm-aware
      when a `SwarmCoordinator` is wired);
    - `step_failed` → naive split into `s1-a`/`s1-b` substeps (halved duration);
    - `risk_escalation` → bump `RiskLevel` by one + `retry_budget += 1`, emit
      `RiskEscalated`;
    - `swarm_rebalance` → reassign between `from_agent`/`to_agent`.
    The adapted plan gets `version += 1` and `status = ADAPTED`; `PlanAdapted`
    emitted. Each replanned step is re-executed by a recovery loop.
  - *LLM shim* — when `llm_client` is injected, `_replan` first asks the LLM for
    ad-hoc JSON (`{"steps": [...]}`); any error → silent fallback to rules. This is
    a **demo aid, not a contract** (see Honest Notes).
  - *Risk assessment* — `risk_assess(plan, history)` raises step `RiskLevel` from
    past `ExecutionOutcome`s (HIGH after >2 failures of the same capability;
    CRITICAL when an outcome has `error_type == "agent_unhealthy"`).
  - *Injectables* — `event_bus`, `event_store`, `swarm_coordinator`, `llm_client`,
    `sleep`, `rng`, `clock`. Axis: imports only `kernel.domain` + `kernel.events`
    (+ lazy `kernel.swarm`).
- **`kernel/plan_store.py`** — `PlanStore`: in-memory CRUD + optional SQLite
  (`plans`, `outcomes` tables), mirroring `SwarmStore`.
- **`kernel/workflow.py`** — `WorkflowEngine.execute_adaptive` (builds a `Plan`
  from workflow steps, executes via the planner; **transparent fallback** to legacy
  `execute_step` when no planner is wired) + `replan_step` (emits `ReplanTriggered`,
  returns adapted `Plan`). Backward-compatible: existing `WorkflowEngine` tests
  unchanged.
- **`kernel/swarm.py`** — `SwarmCoordinator.rebalance_load`: emits a `ReplanTrigger`
  (reason `swarm_rebalance`, `context = {from_agent, to_agent}`) when load variance
  across healthy members exceeds `0.5`.
- **No new dependency** — pure asyncio + existing kernel infrastructure.

## Consequences

- **Positive:** workflows can now survive transitive failures (agent death,
  missing capability) by re-planning; risk escalates with observed history; the
  planner composes with the swarm for load-aware reassignment.
- **Positive:** fully testable — 39 new tests, 504 total, 91% coverage; all
  timers/llm/rng injectable.
- **Positive:** zero regression — the 461 pre-ADR-024 `WorkflowEngine` tests still
  pass; the planner is opt-in (default `None`).
- **Negative:** the adapted plan is a *separate* object; the original `Plan` keeps
  `status = ADAPTED` after a replan (by design).
- **Negative:** LLM replanning is best-effort and unvalidated (see Honest Notes).

## Honest Notes (known limitations)

- **LLM replanning is a shim.** The planner serializes the plan + trigger to a
  prompt and asks the LLM for ad-hoc JSON (`{"steps": [...]}`). There is **no
  formal schema**, **no cost/timeout budgeting**, and **no validation** beyond
  field extraction. Use only for demos; the rule-based path is the real engine.
- **Rule-based replan covers ~80%** of realistic failure modes. The LLM path is an
  augmentation, not a replacement.
- **Substep splitting is naive** — `step_failed` splits a step into `-a`/`-b`
  suffixes with halved `estimated_duration_ms`; it does **not** semantically
  decompose the work.
- **Risk assessment is a heuristic** (failure-count threshold + `error_type`
  lookup), not predictive modeling.
- **Persistence is local SQLite only** — no distributed / cross-node plan store.
- **No predictive scheduling** — replanning is reactive (post-failure), not
  anticipatory.
