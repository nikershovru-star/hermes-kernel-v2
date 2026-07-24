# ADR-028 — Capability Guard (Permission-based Plugin Sandbox)

- **Status:** Accepted
- **Date:** 2026-07-25
- **Deciders:** Hermes Kernel v2 architecture review (v2.14.0)
- **Depends on:** ADR-026 (Plugin Marketplace), ADR-020 (Execution Sandbox),
  ADR-017 (Event Platform)

## Context

ADR-020 introduced an **OS-level-ish** execution sandbox (`kernel.sandbox.Sandbox`)
that enforces *resource* limits (cpu / memory / network / subprocess) on a
running callable. ADR-026 added a distributed **Plugin Marketplace** that
installs packages — but installation carried no *authorization* story: any
installed package could be executed by `AgentRuntime` / `WorkflowEngine` with no
say over *which* capability it may invoke or *how often*.

The pain (from real operations): "we installed a plugin but we can't say
'this agent may call `weather.fetch` but not `secret.spy`'". We needed a
**permission layer** keyed on capability + resource, cooperative (no OS
isolation rewrite), and **optional** (zero regression when unwired).

## Decision

Add a `CapabilityGuard` (ADR-028) — an in-process, **cooperative** permission
sandbox for installed plugin packages:

- **`kernel/security_domain.py`** — permission-based models, deliberately
  *separate* from `kernel.domain.SandboxPolicy` (ADR-020, a different shape).
  - `Permission(action, resource)` — `matches()` does exact action + resource
    wildcard (`*`).
  - `ResourceLimit(cpu_ms, mem_mb, max_calls)` — *soft / cooperative* limits.
  - `SandboxPolicy(permissions, resource_limits)` — one per package.
  - `AuditEntry` — immutable-in-spirit audit record.
- **`kernel/security_store.py`** — `SecurityStore`: in-memory CRUD + optional
  SQLite (`policies` / `grants` / `audit` tables). Mirror of
  `PlanStore` / `GraphStore` / `MarketplaceStore` / `ObservabilityStore`.
- **`kernel/capability_guard.py`** — `CapabilityGuard`:
  - `register_policy(package_id, policy)` / `grant(package_id, permission)` —
    layering of policy + explicit grants.
  - `check(principal, action, resource) -> bool` — allow iff policy OR grant
    matches; an **un-registered** principal is allowed (backward compatible).
  - `wrap(handler, package_id, action, resource)` — async context manager that
    runs `handler` guarded: permission pre-check, cooperative cpu/call accrual,
    audit + domain events (`sec.permission_denied`, `sec.resource_limit_exceeded`,
    `sec.plugin_sandboxed`, `sec.audit_entry`).
  - `call(...)` — convenience wrapper around `wrap`.
  - All I/O injectable (clock / event_bus / event_store / store / sleep) for
    deterministic testing.

### Wiring (axis-clean, optional)

- `PluginMarketplace(guard=...)` — on `install`, registers the package's
  policy with the guard (after allow-list validation of policy actions).
- `AgentRuntime(marketplace=..., guard=...)` — `execute` resolves the package
  backing a capability and wraps the agent call in `guard.wrap`.
- `AgentRuntime.install_capability(...)` — installs a package and registers its
  policy with the guard.
- `WorkflowEngine(..., guard=...)` — `execute_step` wraps the step runner in
  `guard.wrap`; denial / resource breach is converted to a clean error Artifact
  and the **instance is set to FAILED** (see Honest Notes).
- `CapabilityGuard` is **optional**: when `None`, every call is a no-op passthrough.

### Axis contract

`capability_guard` / `security_domain` / `security_store` import only
`kernel.events` / `kernel.security_domain` (leaf). They never import `plugins`
or `mcp`. `kernel.agent` / `kernel.workflow` / `kernel.marketplace` import the
guard **type-only** and *wire* it — no reverse dependency.

## Honest Notes / Limitations (documented, not hidden)

1. **In-process only.** This is NOT OS-level seccomp / cgroup / WASM isolation.
   A malicious package can still escape — the guard is cooperative and trusts
   the package to call `guard.wrap`. It is a *policy* layer, not a *container*.
2. **Cooperative / soft resource limits.** `cpu_ms` is accrued via an injectable
   clock delta inside each `wrap`; `max_calls` is a counter. There is no hard
   OOM-killer or preemption. A package that ignores the guard never hits a limit.
3. **Policy "signature" is basic allow-list validation** in `PluginMarketplace`
   (`_ALLOWED_ACTIONS`), not full PKI / code signing.
4. **Audit log is a ring buffer + SQLite**, not an immutable WORM store.
5. **Denial now FAILS the workflow instance** (was a bug in the first pass: the
   pre-check left `status=RUNNING` on denial, which made the linear fallback
   loop forever). Fixed in v2.14.0 — `WorkflowEngine.execute_step` wraps the
   step in `guard.wrap` and on `PermissionDeniedError` / `ResourceLimitExceededError`
   records `context["permission_denied"]` and sets `WorkflowStatus.FAILED`.

## Consequences

- New vertical "Security Platform" established (permission-based sandbox) on top
  of the existing ADR-020 resource sandbox — two complementary layers.
- Every plugin package can now carry a `policy` (`PluginPackage.policy`).
- Zero regression: with no guard wired, all 619 prior tests still pass; the
  guard adds 30 new tests (649 total).
- `WorkflowInstance` gained a `context: dict` field (backward compatible).

## Tests

- `tests/test_capability_guard.py` — 18 unit tests (policy / grant / check /
  wrap success / deny / resource limits / audit / events).
- `tests/test_security_store.py` — 15 tests (SecurityStore CRUD + SQLite reload).
- `tests/test_security_integration.py` — 13 integration tests (marketplace install
  registers policy, AgentRuntime.execute allow/deny, install_capability,
  WorkflowEngine.discover_plugins filtering, workflow step denial → FAILED).
