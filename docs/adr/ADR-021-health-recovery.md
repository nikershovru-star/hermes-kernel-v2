# ADR-021 — Health & Recovery (Liveness, Dead-Letter, Auto-Recovery, Circuit Breaker)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.7.0)
- **Depends on:** ADR-016 (Agent/Plugin Unification), ADR-017 (Event Platform + CQRS), ADR-019 (Workflow Runtime), ADR-020 (Execution Sandbox)

---

## Context

The v5 Execution Platform layers as: Agent Runtime (✅ v2.2.1), Workflow Runtime
(✅ v2.5.0), Plugin Runtime (✅ v2.0+), Sandbox (✅ v2.6.0), **Health / Recovery
(🆕 this release)**, Swarm / Teams (future, ADR-022).

Until now the kernel could execute, sandbox, and compensate — but had no way to
*observe* whether components stayed alive, no store for *failed* work, and no
*automatic* recovery. Four concrete pains drove this release:

1. **No health visibility** — agents/workflows died silently; "is the desktop
   agent still alive?" had no programmatic answer.
2. **No dead-letter queue** — failed tasks (sandbox breach, exception, timeout)
   were logged and forgotten; no retry-from-checkpoint, replay, or analysis.
3. **No auto-recovery** — a sandbox kill left the agent dead; a stalled workflow
   stayed stalled; no auto-restart, no escalation.
4. **No circuit breaker** — a broken capability (e.g. `pyautogui` on a frozen
   window) was retried infinitely, wasting resources and polluting logs.

## Decision

Introduce `kernel/health.py` with four cooperating primitives (axis contract
`kernel.health → [kernel.domain, kernel.events, kernel.bus]`):

- **`HealthMonitor`** — periodic liveness probes → one `HealthRecord` per
  component; one `asyncio.Task` per probe loop (cancellable on stop/unregister);
  emits `AgentUnhealthy` / `AgentRecovered` on status transitions. Status ladder:
  `UNKNOWN → HEALTHY / DEGRADED → UNHEALTHY` gated by `failure_threshold` /
  `success_threshold`.
- **`DeadLetterQueue`** — append-only store of `DeadLetterEntry` for
  replay/analysis; `append` / `list` / `recover` / `replay(handler)`. Replay is
  **idempotent** — only unrecovered entries are replayed and the handler decides
  success. Emits `DeadLetterAppended` / `DeadLetterRecovered`.
- **`CircuitBreaker`** — per-capability `CLOSED → OPEN → HALF_OPEN → CLOSED`
  state machine. HALF_OPEN admits **exactly one** test call at a time; recovery
  timeout uses an injectable clock for fast tests. Emits `CircuitBreakerTripped`.
- **`RecoveryEngine`** — subscribes to `AgentUnhealthy`; decision tree: agent →
  stop+start; workflow → dead-letter (compensation already ran in the engine);
  max-restarts exceeded → dead-letter + human escalation (log-only). Restart is
  bounded per component so it **cannot infinite-loop**.

Integration is **optional and backward-compatible** (all `None` by default):
`AgentRuntime(health_monitor=…)`, `WorkflowEngine(health_monitor=…,
dead_letter=…)`, `CapabilityExecutor(circuit_breaker=…)`. New events live in
`kernel/events.py`; new entities in `kernel/domain.py`. No new tach module —
`health.py` is under the `kernel` umbrella.

## Consequences

- **+40 tests** (`test_health_monitor.py`, `test_dead_letter.py`,
  `test_circuit_breaker.py`, `test_recovery_engine.py`,
  `test_integration_health.py`) — total **377 passed, 3 skipped, 91% total
  coverage** (up from 89%). `health.py` itself at 96%.
- **No new runtime dependency** — pure `asyncio` + the existing EventBus/EventStore.

### Honest notes (deferred)

- HealthMonitor probes are **in-process** (no external probe endpoint / HTTP).
- DeadLetterQueue is **in-memory + EventStore** (no separate persistence layer).
- RecoveryEngine restart is **stop + start** (not process-level restart).
- Workflow "recovery" currently **dead-letters** the stalled instance rather
  than resuming from a checkpoint (no checkpoint store yet).
- CircuitBreaker is **per-capability** (not global / not per-tenant).
- **No distributed health** (single-node only) → v5 Swarm/Teams (ADR-022).
- **No alerting channel** — human escalation is log-only → future.
