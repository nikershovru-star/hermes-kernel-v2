# ADR-027 — Observability / Metrics

- **Status:** Accepted
- **Date:** 2026-07-24
- **Supersedes / relates:** ADR-007 (core), ADR-025 (semantic memory), ADR-026 (marketplace / multi-node)
- **Version:** v2.13.0

## Context

Hermes Kernel v2 grew a layered runtime (dynamic planner, semantic memory,
plugin marketplace, multi-node cluster). Each layer emits state changes, but
there was no first-class, side-effect-free way to *observe* the running system:
no structured metrics, no correlation-scoped logging, no tracing, and no single
health snapshot. Operators had to read domain events a posteriori.

We needed a self-contained observability layer that:

1. Runs with **zero side effects** (all I/O injectable: `clock`, `rng`,
   `sleep`, `event_bus`, `event_store`, optional `store`).
2. Respects the **axis contract** — imports only its own domain module +
   `kernel.events`; never depends on `workflow`/`agent`/`plugins`/`mcp`.
3. **Integrates** (does not intrude) — `AgentRuntime` / `WorkflowEngine` wire the
   engine in via an optional constructor argument and emit spans/metrics/logs
   lazily (no-op when unwired → full backward compatibility).
4. **Persists** optionally (in-memory + SQLite) mirroring `PlanStore` /
   `GraphStore` / `MarketplaceStore`.

## Decision

Introduce three new modules under the `kernel.observability*` namespace:

| Module | Responsibility |
|--------|----------------|
| `kernel/observability_domain.py` | Pydantic domain models: `MetricType` (counter/histogram/gauge), `MetricRecord`, `TraceSpan`, `LogEntry`. Isolated from `kernel.domain` for a clean axis. |
| `kernel/observability.py` | `ObservabilityEngine` — ring-buffered metrics/spans/logs, health snapshot, Prometheus exposition export. |
| `kernel/observability_store.py` | `ObservabilityStore` — in-memory CRUD + optional SQLite (`metrics`/`spans`/`logs` tables). Repo-loads on `db_path`. |

### Public API (`ObservabilityEngine`)

- `record_metric(name, value, labels=None, mtype=MetricType.COUNTER) -> MetricRecord`
  — appends to ring buffer, accumulates counters, persists (if store), emits
  `MetricRecorded`.
- `start_span(trace_id, span_name, parent_id=None, correlation_id=None) -> span_id`
- `finish_span(span_id, status="ok") -> TraceSpan | None`
- `get_trace(trace_id) -> list[TraceSpan]` (sorted by start time)
- `log(level, message, correlation_id=None, context=None) -> LogEntry`
- `get_logs(correlation_id=None, level_min="debug") -> list[LogEntry]`
- `get_health_snapshot() -> dict` — uptime, counters, last_error, error_count,
  buffered counts, active span count.
- `export_metrics() -> str` — Prometheus exposition text (cumulative counters +
  latest gauge/histogram sample).

Ring buffers cap at `metrics_limit` / `logs_limit` / `spans_limit` (default 1000),
evicting oldest first. All time comes from an injectable `clock` (defaults to
`datetime.now(timezone.utc)`).

### Events (ADR-027, added to `kernel/events.py`)

`MetricRecorded`, `TraceSpanStarted`, `TraceSpanFinished`, `LogEntryEmitted`
(namespaced `obs.*`). Published on the bus and appended to the `EventStore` when
present; engine degrades gracefully if neither is configured.

### Integration points (no behavior change when unwired)

- `AgentRuntime(..., observability=None)`: logs `agent started`, wraps
  `execute()` in a `agent.execute` span, records `agent.executions`, logs
  `agent execution failed` on exception, logs + counts `agent.capability_installs`
  on `install_capability`; exposes `get_health()`.
- `WorkflowEngine(..., observability=None)`: spans `execute_adaptive` /
  `execute_with_context`, records `wf.executions` / `wf.steps_total` /
  `wf.errors` / `wf.kg_matches`, logs `workflow started` / `workflow executed` /
  `workflow execution failed`.

Both default the engine to `None`, so existing call sites (and the 589 prior
tests) are unaffected.

### Axis contract compliance

`kernel/observability.py` imports **only** `kernel.observability_domain` +
`kernel.events`. `kernel/observability_store.py` imports only
`kernel.observability_domain`. `kernel/observability_domain.py` imports only
stdlib + `pydantic`. No reverse dependency on workflow/agent/plugins/mcp.

Protected files (`sandbox.py`, `discovery.py`, `bus.py`, `planner.py`,
`registry.py`) were **not** modified.

## Consequences

- **+30 new tests** (`test_observability.py`, `test_observability_store.py`,
  `test_observability_integration.py`): counters, gauge/histogram, span
  open/close + get_trace, level-filtered log query, health snapshot, Prometheus
  export shape, SQLite persistence + reload, agent/workflow wiring (no-op when
  unwired + spans/metrics when wired), store round-trip.
- Total suite: **619 passed, 3 skipped**.
- Kernel coverage: **92%** (`observability.py` 92%, `observability_store.py`
  98%, `observability_domain.py` 100%).
- `tach check`: green.
- New public exports: `ObservabilityEngine`, `ObservabilityStore`, `MetricRecord`,
  `TraceSpan`, `LogEntry`, `MetricType`.
- **Limitations (documented):** Prometheus export emits cumulative counters and
  the latest gauge/histogram sample — no histogram bucket math. Ring buffers are
  not an LTS store; pair with `ObservabilityStore` (SQLite) for durability.

## Release

- Commit: `feat(obs): Observability / Metrics — engine, store, events, agent+workflow wiring (ADR-027, v2.13.0)`
- Tag: `v2.13.0`
- Baseline carried: 589 → 619 (+30), coverage 92%, tach green.
