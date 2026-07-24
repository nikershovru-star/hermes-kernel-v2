# Hermes Kernel v2 — Roadmap

_Last updated: 2026-07-23 · **v2.5.0** · 320 passed, 3 skipped, 89% total coverage (CI gate ≥85% ✅)_

| Phase | Название | Статус | Этапы | Gate |
|-------|----------|--------|-------|------|
| P0 | Kernel Core | ✅ | domain, bus, registry, plugins | 62 passed, 94% |
| P1 | Runtime + Capability | ✅ | capability, executor, workspace, integration | 108 passed, 95% |
| P2 | Knowledge Pipeline | ✅ | scanner, parser, chunker, embedder, graph + e2e | 140 passed, 90.5% |
| P3 | MCP + SDK | ✅ | client, sdk | 113 passed, 90% |
| P4 | MCP Server | ✅ | server, tools, **SSE + Streamable HTTP transport** | 37 MCP tests green |
| P5 | Multi-tenancy | ✅ | auth, rbac, persistence | 157 passed, ~91% |
| A | SSE transport | ✅ | `mcp/server_sse.py` | 5 tests, 80% |
| A2 | Streamable HTTP transport (durable sessions) | ✅ | `mcp/server_streamable.py` | 10 tests, 88% |
| B | KnowledgeRetrievalService | ✅ | `kernel/retrieval.py` | 4 tests, 96% |
| C | Plugin SDK CLI | ✅ | `plugins/sdk/cli.py` (`hermes plugin` init/watch/**list/validate/disable**) | 16 tests |
| D | ADR-007 Workspace Isolation | ✅ | `docs/adr/ADR-007-*.md` | spec |
| ADR-008 | Streamable HTTP Transport | ✅ | `docs/adr/ADR-008-*.md` | spec |
| ADR-009 | Retrieval Backends | ✅ | `kernel/retrieval_backends.py` | 17 tests, 68%* |
| ADR-010 | Plugin CLI UX | ✅ | `plugins/sdk/validator.py` + `kernel/registry.PluginRegistry` | 13 tests, 84/87% |
| ADR-011 | Desktop Control | ✅ | `plugins/builtin/desktop_control` | 10 tests, 88% |
| ADR-012 | MCP Streamable hardening | ✅ | `mcp/server_streamable.py` | 6 tests, 86% |
| ADR-013 | Human Emulation Layer | ✅ | `plugins/builtin/human_emulation/` | 18 tests, 86% |
| ADR-016 | Agent/Plugin Unification | ✅ | `kernel/agent.py` + `kernel/capability.py` + `plugins/builtin/agents/` | 14 tests |
| ADR-017 | Event Platform + Desktop Agent Vision | ✅ | `kernel/events.py` + `plugins/builtin/desktop_control/` | 31 tests, 88% |
| ADR-018 | Capability Handler Auto-Discovery | ✅ | `kernel/discovery.py` + `kernel/capability.py` | 6 tests |
| ADR-019 | Workflow Runtime Foundation | ✅ | `kernel/workflow.py` + `kernel/planner.py` + `kernel/domain.py` | 23 tests, 89% |
| ADR-020 | Execution Sandbox | ✅ | `kernel/sandbox.py` + `kernel/domain.py` + `kernel/events.py` | 17 tests, 89% |

> \* `kernel/retrieval_backends.py` coverage is **68% on Windows** (sqlite-vss
> has no Windows wheels → its 3 tests skip). On Linux with `faiss-cpu` +
> `sqlite-vss` installed, file coverage reaches **~87%**. Project total stays
> ≥92%.

> **P5 = COMPLETE.** Auth (P5.1), RBAC (P5.2), Persistent Storage (P5.3) — see
> [ADR-005](adr/ADR-005-multi-tenancy.md). Milestone tag: **`v0.7.0`**.
> Post-milestone extensions A–D + A2 + ADR-008 + ADR-009 delivered
> (tag **`v0.8.0`** → **`v0.9.1`**).

## P2 Knowledge Pipeline (delivered)

Five event-driven, workspace-scoped stages — see
[ADR-004](adr/ADR-004-knowledge-pipeline.md):

```
FileScanner → document.scanned → DocumentParser → document.parsed
  → DocumentChunker → chunk.created → ChunkEmbedder → chunk.embedded
    → KnowledgeGraph → graph.updated
```

End-to-end verified on a live `.md` file (`tests/test_integration_p2.py`):
**10 nodes, 8 similarity edges, `workspace_id` intact across all 5 hops.**

## P5 Multi-tenancy (delivered)

Three cooperating layers — see [ADR-005](adr/ADR-005-multi-tenancy.md):

| Layer | Module | Responsibility | Cov |
|-------|--------|----------------|-----|
| Auth | `kernel/auth.py` | `User`, `AuthRegistry`, pbkdf2-hash passwords | 100% |
| RBAC | `kernel/rbac.py` | `Permission`, `Role`, `RBACRegistry` guard | 81% |
| Persistence | `kernel/persistence.py` | SQLite async CRUD, workspace-isolated | 82% |

Integration: `WorkspaceRegistry` (save/load), `KnowledgeGraph` (persist/load),
`FileScanner` (DB-backed de-duplication) all compose with `PersistenceRegistry`
without rewriting the registries.

## Extensions A–D (delivered this session)

- **A — SSE transport** (`mcp/server_sse.py`): GET `/sse` + POST `/messages/`
  over stdlib `http.server`; reuses `MCPServer` JSON-RPC core, own asyncio
  loop on a worker thread. No FastAPI/uvicorn. Tests in `tests/test_mcp_sse.py`.
- **B — KnowledgeRetrievalService** (`kernel/retrieval.py`): durable,
  workspace-scoped cosine vector search over `KnowledgeNode` embeddings stored
  in `PersistenceRegistry`; in-memory index + event-driven auto-index on
  `graph.updated`. Tests in `tests/test_retrieval.py`.
- **C — Plugin SDK CLI** (`plugins/sdk/cli.py`): `hermes plugin init <name>`
  scaffolds a plugin (`.py` + `plugin.yaml`); `hermes plugin watch <dir>`
  hot-reloads changed modules via polling (no watchdog dep). Wired as a
  `[project.scripts]` console entry point. Tests in `tests/test_sdk_cli.py`.
- **D — Desktop Control builtin plugin** (`plugins/builtin/desktop_control/`):
  `DesktopControlPlugin(BasePlugin)` exposes mouse/keyboard/screenshot as
  `hermes.desktop` Tools (`mouse_move`, `mouse_click`, `key_press`, `type_text`,
  `screenshot`). Lazy `pyautogui`/`Pillow` (installed via `[desktop]` extra),
  `asyncio.to_thread` for blocking calls, platform guard in `load()`.
  Tests: `tests/test_desktop_control.py` (10 tests, 88% cov). See **ADR-011**.
- **D — ADR-007** (`docs/adr/ADR-007-workspace-isolation.md`): formalises the
  already-enforced workspace isolation contract (persistence, graph, retrieval,
  scanner, RBAC).

## Current test surface (212 tests, 3 skipped)

| Suite | Tests | Suite | Tests |
|-------|-------|-------|-------|
| test_domain | 27 | test_registry | 9 |
| test_plugin_registry | 13 | test_loader_errors | 12 |
| test_bus | 8 | test_capability | 8 |
| test_mcp | 8 | test_mcp_client | 8 |
| test_executor | 6 | test_graph | 6 |
| test_loader | 5 | test_sdk | 5 |
| test_workspace | 5 | test_scanner | 6 |
| test_parser | 5 | test_chunker | 5 |
| test_embedder | 4 | test_auth | 6 |
| test_rbac | 4 | test_persistence | 6 |
| test_integration | 3 | test_integration_p2 | 1 |
| test_mcp_sse | 5 | test_retrieval_backends | 17 (+3 skip) |
| test_sdk_cli | 16 | test_mcp_streamable | 10 |

## Module coverage snapshot

| Module | Cov | Module | Cov |
|--------|-----|--------|-----|
| kernel/domain.py | 100% | kernel/capability.py | 97% |
| kernel/auth.py | 100% | kernel/workspace.py | 97% |
| kernel/graph.py | 96% | kernel/scanner.py | 96% |
| kernel/chunker.py | 94% | kernel/executor.py | 92% |
| kernel/registry.py | 84% | kernel/bus.py | 90% |
| kernel/parser.py | 84% | kernel/embedder.py | 84% |
| kernel/rbac.py | 81% | kernel/persistence.py | 82% |
| mcp/tools.py | 97% | mcp/client.py | 81% |
| mcp/server.py | 81% | mcp/server_sse.py | 80% |
| mcp/server_streamable.py | 88% | plugins/loader.py | 97% |
| plugins/sdk/cli.py | 84% | plugins/sdk/validator.py | 87% |
| kernel/retrieval.py | ~88% | kernel/retrieval_backends.py | 68%* |

> \* 68% on Windows (sqlite-vss unavailable → VSS branch skips); ~87% on Linux
- **v2.5.0 tagged** ✅ — Workflow Runtime Foundation (ADR-019): `kernel/workflow.py`
  (`WorkflowEngine` state machine — retry/backoff, reverse-order compensation,
  human-approval PAUSE, input-mapping from prior steps, every transition emits a
  `DomainEvent`), `kernel/planner.py` (rule-based goal→`Workflow`), `Workflow`
  domain model replaces the stub and **activates the dead `Task.workflow_id`**
  field. `AgentRuntime.execute(..., workflow_id=None)` now propagates it. 23 new
  tests (320 total, 89.41% coverage).
  - **What hurts now (deferred → next ADRs):** Planner is **rule-based only**
    (no LLM/reasoning → ADR-023); compensation is **reverse-order step
    execution**, not a full Saga (future); human approval is **in-memory PAUSED
    state** (no external approval service/UI → future KG visualizer); workflows
    are **single-node** (no distributed execution → v5 Swarm/Teams ADR-022);
    DAG is executed as an **ordered step list** (no parallel/conditional
    branching yet).

## Next up
- **v2.6.0 — ADR-020: Sandbox** (plugin execution isolation / resource limits)
- v2.7.0 — ADR-021: Health & Recovery (liveness, dead-letter, auto-restart)
- v2.8.0 — ADR-022: Swarm / Teams (multi-agent orchestration)
- v2.9.0 — ADR-023: Dynamic Planner (LLM-based replanning)
- **Knowledge graph visualization (web UI)** — future.
- **Plugin marketplace / remote install** — future.
- **Multi-node distributed kernel** — future.

See the [ADRs](adr/) for architectural decisions behind each phase.
