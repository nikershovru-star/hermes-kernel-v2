# ADR-020 — Execution Sandbox (Resource Limits, Timeout, Isolation)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.6.0)
- **Depends on:** ADR-016 (Agent/Plugin Unification), ADR-017 (Event Platform + CQRS), ADR-019 (Workflow Runtime)

---

## Context

The v5 Execution Platform decomposes into: Agent Runtime (✅ v2.2.1), Workflow
Runtime (✅ v2.5.0), Plugin Runtime (✅ v2.0+), **Sandbox (🆕 this release)**,
Health / Recovery / Swarm (future).

Until now, agents, workflow steps, and capabilities executed as unbounded
in-process coroutines. A misbehaving plugin (infinite loop, memory leak, a
`pyautogui` call blocking on a frozen window) could hang or exhaust the host
with no enforced ceiling. Three pains drove this release:

1. **No timeout enforcement** — a stuck coroutine hangs the runtime forever.
2. **No resource ceiling** — no CPU/memory/file-descriptor limits; a leaky
   plugin degrades the whole process.
3. **No breach signal** — when something misbehaves there is no event, no
   cleanup hook, no auditable record of the violation.

## Decision

Introduce `kernel/sandbox.py` providing **soft**, in-process enforcement:

- **`Sandbox.run(coro, policy, ...)`** — wraps any coroutine with a
  `SandboxPolicy` (timeout, CPU, memory, file descriptors, network/subprocess
  intent flags).
- **`TimeoutGuard`** — real enforcement via `asyncio.wait_for` + cancellation.
- **`ResourceMonitor`** — best-effort CPU/memory/fd sampling via `psutil` when
  available; degrades gracefully to timeout-only when `psutil` is absent (never
  crashes on a missing optional dependency).
- **`SandboxError`** — raised on any breach, carrying the `SandboxViolation`.
- **Domain model** (`kernel/domain.py`): `SandboxPolicy`, `SandboxViolation`.
- **Events** (`kernel/events.py`): `SandboxViolationEvent`,
  `SandboxCleanupCompleted` — every breach emits both, reusing the existing
  EventBus + EventStore (ADR-017).
- **Integration** (optional, backward-compatible):
  - `AgentRuntime(sandbox=...)` — sandboxed `execute()`; breach cancels + stops
    the agent via cleanup hook.
  - `WorkflowEngine(sandbox=...)` — sandboxed step execution; `SandboxError`
    routes into the existing retry/compensation machinery.

`AXIS CONTRACT`: `kernel.sandbox → [kernel.domain, kernel.events]`. Never
imports plugins. Clean under `tach check`.

## Consequences

- **+17 tests** (`test_sandbox.py`, `test_sandbox_integration.py`) — total
  **337 passed, 3 skipped, 89% total coverage**.
- **No hard new dependency** — `psutil` is an optional extra (`[monitor]`);
  absent it, the sandbox still enforces timeout.

### Honest notes (deferred)

- Enforcement is **soft / in-process** — NOT a process or container sandbox.
  There is no `subprocess` isolation, no `seccomp`, no firewall → deferred to a
  future process-level ADR (ADR-024).
- CPU / memory / fd limits are **best-effort sampled**, not hard `rlimit`
  ceilings — a burst between samples can exceed the limit briefly.
- Network / subprocess policy fields are **intent flags only** (recorded +
  surfaced via events); no active blocking yet.
- Single-node only — no distributed resource accounting.
