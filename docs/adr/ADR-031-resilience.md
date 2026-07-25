# ADR-031 — Resilience Platform (Circuit Breaker, Retry, Dead Letter Queue)

- **Status:** Accepted
- **Date:** 2026-07-26
- **Version:** v2.17.0
- **Depends on:** ADR-029 (MCP Gateway — external call surface), ADR-030
  (Config Vault — optional secret wiring), ADR-021 (Health Recovery — a
  *distinct* layer, see "Relationship to ADR-021" below).

## Context

External MCP servers crash, plugins time out, and a single failing workflow
step can cascade into a full instance failure. The kernel needs **execution
resilience** primitives so that:

1. Repeated failures against a flaky dependency trip a **circuit breaker** that
   fails fast (no piling on a dead dependency).
2. Transient failures are retried with **deterministic backoff** before giving
   up.
3. Terminally-failed tasks are **parked in a dead-letter queue** for manual
   replay instead of being silently dropped.

These concerns are cross-cutting: they apply uniformly to MCP calls
(ADR-029), workflow steps (ADR-019/024), and agent execution (ADR-022).

### Relationship to ADR-021 (Health Recovery)

ADR-021 already ships health-probe driven recovery (`kernel/health.py`:
`CircuitBreaker`, `DeadLetterQueue`, `RecoveryEngine`) and `kernel/domain.py`
already defines `RetryPolicy` and `DeadLetterEntry`. This ADR is **deliberately
distinct and isolated**:

- ADR-021 = *health/recovery* (proactive liveness probes, auto-recovery).
- ADR-031 = *execution resilience* (guarding live calls with fail-fast +
  retry + DLQ at the call site).

To avoid name collisions with the existing `domain.RetryPolicy` /
`domain.DeadLetterEntry`, **every new type here is prefixed `Resilience*`**
(`ResilienceRetryPolicy`, `ResilienceDeadLetterEntry`, `ResilienceCircuitConfig`)
and lives in its own isolation module `kernel/resilience_domain.py`. Nothing in
this ADR reuses or modifies the ADR-021 types, preserving zero regression.

## Decision

Three isolated, axis-clean modules:

### `kernel/resilience_domain.py` (isolated models)
- `CircuitState` — `CLOSED | OPEN | HALF_OPEN`.
- `ResilienceCircuitConfig` — `name`, `failure_threshold`, `recovery_timeout_ms`,
  `half_open_max_calls`.
- `ResilienceRetryPolicy` — `max_attempts`, `backoff_base_ms`, `max_backoff_ms`,
  `retryable_exceptions` (allow-list); deterministic exponential backoff
  (`backoff_base_ms * 2**(attempt-1)`, capped at `max_backoff_ms`).
- `ResilienceDeadLetterEntry` — `entry_id`, `original_task`, `error`, `attempts`,
  `enqueued_at`, `last_attempt`, `status` (`pending|replayed|discarded`).
- `CircuitBreakerOpenError`, `RetryExhaustedError` — typed failures.

### `kernel/resilience.py` — `ResilienceEngine` (async, injectable)
- `register_circuit(name, config)` — create/replace a breaker.
- `call_with_circuit(name)` — **async context manager**: CLOSED executes; OPEN
  raises `CircuitBreakerOpenError` immediately; HALF_OPEN admits up to
  `half_open_max_calls` trial calls, success → CLOSED, failure → OPEN. Recovery
  OPEN→HALF_OPEN is clock-driven (`recovery_timeout_ms`). Emits
  `CircuitBreakerOpened` / `CircuitBreakerClosed`.
- `retry(coro_factory, policy, task_id)` — runs `coro_factory()` with retry +
  deterministic backoff, emits `RetryAttempted` per attempt, `RetryExhausted`
  + raise `RetryExhaustedError` on exhaustion. Only `retryable_exceptions`
  are retried.
- `enqueue_dead_letter(...)` / `replay_dead_letter(...)` /
  `discard_dead_letter(...)` / `list_dead_letter(status)` — DLQ lifecycle.
- `get_circuit_status(name)` — current state.
- Injectable: `store`, `event_bus`, `event_store`, `clock`, `sleep`, `metrics`.

### `kernel/resilience_store.py` — `ResilienceStore`
SQLite tables `circuits` / `retries` / `dead_letter`, plus a pure in-memory
fallback when `db_path=None`. `reload(db_path)` re-points the store (repo-reload).

### Events (5, namespaced `res.*`)
`CircuitBreakerOpened`, `CircuitBreakerClosed`, `RetryAttempted`,
`RetryExhausted`, `DeadLetterEnqueued`.

### Integration (optional, zero regression)
All consumers take `resilience: ResilienceEngine | None = None`. When `None`,
behavior is byte-for-byte the pre-ADR-031 path.

- `McpGateway(resilience=)` — `call_tool` wrapped in
  `call_with_circuit(f"mcp:{server_url}")` + `retry`. Circuit-open /
  retry-exhausted map to a deterministic error `Artifact` (never an unhandled
  crash).
- `WorkflowEngine(resilience=)` — each `_run_step` guarded by
  `call_with_circuit(f"wf:{workflow_id}:{capability}")`; on the existing
  retry-exhaustion path the step is additionally parked in the resilience DLQ
  (distinct from the ADR-021 DLQ already appended).
- `AgentRuntime(resilience=)` — `execute` runs through `resilience.retry`;
  `get_circuit_status(name)` proxies to the engine (raises `RuntimeError` when
  unwired).

## Consequences

- **Positive:** uniform, opt-in execution resilience across MCP/Workflow/Agent
  with no forced coupling; fully deterministic and testable; honest, isolated
  types that do not disturb ADR-021; tach green; axis-clean (imports only
  `resilience_domain` + `events`).
- **Negative / honest limitations:**
  - *Circuit state is in-memory per node* — there is **no distributed consensus**;
    each process maintains its own breaker (no etcd/Consul/Raft).
  - *Backoff is deterministic exponential, without jitter* — bursty synchronized
    retries are possible under shared recovery timeouts.
  - *DLQ replay is manual / triggered* — there is **no automatic poller**; someone
    must call `replay_dead_letter`.
  - *This is execution resilience, not isolation* — it does **not** replace the
    ADR-020 sandbox (no resource/network isolation).
- **Migration:** none — fully additive; existing 753 tests unchanged.
