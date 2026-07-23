# ADR-019 — Workflow Runtime Foundation

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review (v2.5.0)
- **Depends on:** ADR-017 (Event Platform + CQRS), ADR-018 (Capability Auto-Discovery)

---

## Context

The v5 Execution Platform decomposes into: Agent Runtime (✅ v2.2.1), Workflow
Runtime (🆕 this release), Plugin Runtime (✅ v2.0+), Sandbox / Health / Swarm
(future). Until now the kernel could only execute **individual** `Task`s via
`agent.execute(task)`. There was no way to express:

> "Take screenshot → OCR find 'Submit' → Click it → Verify → Retry on failure"

Four concrete pains drove this release:

1. **No orchestration** — steps are manual, error-prone, no rollback, no state.
2. **No state machine** — long-running desktop sequences (login, form-fill)
   have no recoverable state or human-approval checkpoints.
3. **No planner in kernel** — the v5 Cognitive System has Intent→Planner, but
   the kernel had nothing to turn a goal into a `Workflow`.
4. **Dead schema** — `Task.workflow_id` (domain.py) existed but was never used;
   no `Workflow` entity worth the name (a stub with `steps: list[str]`).

## Decision

Introduce a **Workflow Runtime** built on existing, already-validated
primitives (no new architectural layers, axis preserved):

- **`kernel/domain.py`** — replaced the stub `Workflow` with a full
  `Workflow` (DAG of `WorkflowStep`, `WorkflowStatus` enum, `trigger`,
  `context`), plus `WorkflowStep`, `WorkflowInstance`, `RetryPolicy`,
  `WorkflowTrigger`. `Task.workflow_id` is now **activated** (linked on every
  step's `Task`).
- **`kernel/events.py`** — five new `DomainEvent` subclasses
  (`WorkflowStepStarted/Completed/Failed/AwaitingApproval/Compensating`) that
  reuse the existing `EventBus` + `EventStore` (ADR-017).
- **`kernel/workflow.py`** — `WorkflowEngine`: a state machine that
  **uses** `AgentRuntime` (to run steps via a `BaseAgent`) and
  `CapabilityExecutor` (direct capability calls), resolves input mappings
  from previous step results, applies retry/backoff, runs compensation in
  reverse order, and pauses for human approval. Every transition emits a
  `DomainEvent`.
- **`kernel/planner.py`** — `Planner`: rule-based goal→`Workflow` generation
  via capability templates. The v5 Cognitive Planner (LLM + reasoning) is
  explicitly **out of scope** (ADR-023).
- **`kernel/agent.py`** — `AgentRuntime.execute(agent_id, task, workflow_id=None)`
  now propagates `workflow_id` onto `task.workflow_id` (activates the dead field).

### Axis contracts (tach — all validated)

```
kernel.domain       → []
kernel             → [kernel.domain]            (umbrella; workflow/planner are files within)
kernel.events      → [kernel.domain, kernel.bus]
kernel.agent       → [kernel.domain, kernel.events]
kernel.capability  → [kernel.domain, kernel.events, kernel.agent, kernel.discovery]
kernel.workflow    → part of kernel (uses domain/events/agent/capability)
kernel.planner     → part of kernel (uses domain/capability)
NO kernel → plugins imports.
```

`WorkflowEngine` depends on `AgentRuntime`, `CapabilityExecutor`, `EventBus`,
`EventStore` — all kernel-internal. The engine is a **consumer** of
`AgentRuntime`, not a replacement for it (mirrors ADR-017: events layer
consumes the bus).

## Consequences

- **+23 tests** (320 total passed, 3 skipped; coverage 89.41%).
- `Task.workflow_id` is no longer dead — every workflow step's `Task` carries
  the `WorkflowInstance.id`.
- Desktop automation can now be expressed as a recoverable, event-emitting DAG.
- Planner gives a first-class "goal → workflow" path inside the kernel.

## Honest notes (deferred to future ADRs)

- **Planner is rule-based** (template + capability lookup), not LLM/reasoning.
  Dynamic replanning on failure → **ADR-023**.
- **Compensation is reverse-order step execution**, not a full Saga pattern
  (no distributed transactions, no compensating transactions registry) → future.
- **Human approval is an in-memory PAUSED state**; no external approval service
  / UI → future (Knowledge Graph platform visualizer).
- **Single-node only** — no distributed workflow execution; multi-node is v5
  Swarm/Teams (ADR-022).
- **DAG is executed as an ordered step list** for v2.5.0 (linear scan +
  `_advance`); true graph scheduling / branching (parallel steps, conditional
  transitions) is a future enhancement.
- `Workflow` (the old stub) was **replaced**, not extended, because its
  `steps: list[str]` shape was incompatible with the `WorkflowStep` model.
  `test_domain.py` was updated to the new `Workflow(name=...)` + added
  `WorkflowInstance(workflow_id=..., status=...)` case.

## Files

| File | Change |
|------|--------|
| `kernel/domain.py` | `Workflow` (replaced), +`WorkflowStep`, `WorkflowInstance`, `WorkflowStatus`, `RetryPolicy`, `WorkflowTrigger` |
| `kernel/events.py` | +5 `Workflow*` `DomainEvent` subclasses |
| `kernel/workflow.py` | NEW `WorkflowEngine` |
| `kernel/planner.py` | NEW `Planner` |
| `kernel/agent.py` | `execute(..., workflow_id=None)` propagation |
| `tests/test_workflow_engine.py` | NEW (14 tests) |
| `tests/test_planner.py` | NEW (7 tests) |
| `tests/test_workflow_events.py` | NEW (3 tests) |
| `tests/test_domain.py` | updated for new `Workflow` / `WorkflowInstance` |
