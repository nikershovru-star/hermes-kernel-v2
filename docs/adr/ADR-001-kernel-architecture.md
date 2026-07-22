# ADR-001 — Kernel Architecture

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Supersedes:** —

## Context

Hermes Kernel v2 is an async-first, event-driven knowledge-OS kernel. It must
stay maintainable as plugins, MCP integrations, and a knowledge pipeline are
layered on top. The primary risk in such systems is *dependency rot*: high-level
modules leaking into low-level ones until the core is impossible to test or
reason about. We therefore commit to **Clean Architecture** with a strict,
CI-enforced axis of dependency pointing inward toward the domain.

## Decision

### Layers (axis of dependency → inward)

```
        ┌─────────────────────────────────────────────┐
        │                  domain.py                   │  ← no imports from kernel.*
        │  BaseEntity + 13 entities + Capability +     │
        │  PluginManifest (Pydantic BaseModel)         │
        └─────────────────────────────────────────────┘
                          ▲
        ┌─────────────────┴───────────────────────────┐
        │                   bus.py                      │  ← imports domain only
        │  async EventBus: subscribe / publish /        │
        │  wait_for (sync barrier) / close              │
        └───────────────────────────────────────────────┘
                          ▲
        ┌─────────────────┴───────────────────────────┐
        │        registry.py · capability.py            │  ← import domain
        │  PluginRegistry · ToolRegistry ·              │
        │  CapabilityRegistry · AgentRegistry           │
        └───────────────────────────────────────────────┘
                          ▲
        ┌─────────────────┴───────────────────────────┐
        │         executor.py · workspace.py            │  ← import domain + registries
        │  Task state machine · WorkspaceRegistry       │
        └───────────────────────────────────────────────┘
```

| Layer | Module | Responsibility | Coverage |
|-------|--------|----------------|----------|
| Domain | `kernel/domain.py` | Entities (Pydantic `BaseModel`): `Document`, `Chunk`, `Tool`, `Task`, `Event`, `Capability`, `Agent`, `PluginManifest`, … | 100% |
| Bus | `kernel/bus.py` | Async `EventBus`: fire-and-forget `publish`, `wait_for` sync barrier, fault containment per handler | 90% |
| Registry | `kernel/registry.py` | `PluginRegistry`, `ToolRegistry`, `AgentRegistry` (name→entity, async lock + sync fast-path) | 92% |
| Capability | `kernel/capability.py` | `CapabilityRegistry`: bind capabilities → tools (lazy prefix-match resolution) | 97% |
| Executor | `kernel/executor.py` | `Task` state machine `PENDING→QUEUED→RUNNING→COMPLETED\|FAILED`, capability-driven routing | 92% |
| Workspace | `kernel/workspace.py` | `WorkspaceRegistry`: CRUD + active-workspace, auto-seed `default` | 97% |

### Patterns

- **Clean Architecture** — dependency axis points inward; `domain` imports nothing
  from `kernel.*`. Enforced in CI via `import-linter` (see `pyproject.toml`).
- **Event-driven** — every meaningful state change publishes an `Event`; the bus
  is the integration seam between layers.
- **async / lock** — every registry mutation is guarded by `asyncio.Lock`; a
  synchronous fast-path (`register_sync`, `get_by_name_sync`) exists for
  main-thread construction (used by the Plugin SDK, see ADR-002).
- **Eventual consistency + sync barrier** — `publish` is fire-and-forget (returns
  `None`); critical paths await `bus.wait_for([...])` to synchronise.

## Consequences

**Positive**

- The domain is pure and 100%-covered; it can be serialised to JSON / EventBus /
  MCP without adapters (UUIDs are stored as strings for this reason).
- Layers are independently testable — 113 tests, 90% total coverage.
- CI gate (`import-linter`) fails the build on any inward-axis violation.

**Negative / trade-offs**

- Dual async/sync registry API adds surface area (`register` + `register_sync`).
  Justified: SDK constructors run on the main thread and cannot `await`.
- `publish` being non-awaitable is a common footgun (`await bus.publish(...)` is a
  bug); mitigated by explicit docs and tests.

## Related

- [ADR-002 — Plugin System](ADR-002-plugin-system.md)
- [ADR-003 — MCP Integration](ADR-003-mcp-integration.md)
- [Roadmap](../roadmap.md)
