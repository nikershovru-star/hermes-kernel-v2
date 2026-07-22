# Hermes Kernel v2 — Roadmap

_Last updated: 2026-07-23 · **189 passed, 3 skipped, ~92% total coverage**_

| Phase | Название | Статус | Этапы | Gate |
|-------|----------|--------|-------|------|
| P0 | Kernel Core | ✅ | domain, bus, registry, plugins | 62 passed, 94% |
| P1 | Runtime + Capability | ✅ | capability, executor, workspace, integration | 108 passed, 95% |
| P2 | Knowledge Pipeline | ✅ | scanner, parser, chunker, embedder, graph + e2e | 140 passed, 90.5% |
| P3 | MCP + SDK | ✅ | client, sdk | 113 passed, 90% |
| P4 | MCP Server | ✅ | server, tools, **SSE + Streamable HTTP transport** | 37 MCP tests green |
| P5 | Multi-tenancy | ✅ | auth, rbac, persistence | 157 passed, ~91% |
| A | SSE transport | ✅ | `mcp/server_sse.py` | 5 tests, 80% |
| A2 | Streamable HTTP transport | ✅ | `mcp/server_streamable.py` | 7 tests, 82% |
| B | KnowledgeRetrievalService | ✅ | `kernel/retrieval.py` | 4 tests, 96% |
| C | Plugin SDK CLI | ✅ | `plugins/sdk/cli.py` (`hermes plugin`) | 3 tests |
| D | ADR-007 Workspace Isolation | ✅ | `docs/adr/ADR-007-*.md` | spec |
| ADR-008 | Streamable HTTP Transport | ✅ | `docs/adr/ADR-008-*.md` | spec |
| ADR-009 | Retrieval Backends | ✅ | `kernel/retrieval_backends.py` | 17 tests, 68%* |

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
- **D — ADR-007** (`docs/adr/ADR-007-workspace-isolation.md`): formalises the
  already-enforced workspace isolation contract (persistence, graph, retrieval,
  scanner, RBAC).

## Current test surface (189 tests, 3 skipped)

| Suite | Tests | Suite | Tests |
|-------|-------|-------|-------|
| test_domain | 27 | test_registry | 9 |
| test_loader_errors | 12 | test_mcp_transport | 9 |
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
| test_sdk_cli | 3 | test_mcp_streamable | 7 |

## Module coverage snapshot

| Module | Cov | Module | Cov |
|--------|-----|--------|-----|
| kernel/domain.py | 100% | kernel/capability.py | 97% |
| kernel/auth.py | 100% | kernel/workspace.py | 97% |
| kernel/graph.py | 96% | kernel/scanner.py | 96% |
| kernel/chunker.py | 94% | kernel/executor.py | 92% |
| kernel/registry.py | 92% | kernel/bus.py | 90% |
| kernel/parser.py | 84% | kernel/embedder.py | 84% |
| kernel/rbac.py | 81% | kernel/persistence.py | 82% |
| mcp/tools.py | 97% | mcp/client.py | 81% |
| mcp/server.py | 81% | mcp/server_sse.py | 80% |
| mcp/server_streamable.py | 82% | plugins/loader.py | 97% |
| plugins/sdk/cli.py | 64% | kernel/retrieval.py | ~88% |
| kernel/retrieval_backends.py | 68%* | | |

> \* 68% on Windows (sqlite-vss unavailable → VSS branch skips); ~87% on Linux
> with both `faiss-cpu` + `sqlite-vss` installed.

## Next up

- **MCP Streamable HTTP durable sessions**: `Last-Event-ID` replay + session
  persistence via `PersistenceRegistry` (currently in-memory only).
- **Plugin CLI UX**: `hermes plugin list`, `validate`, `disable`.
- **CI tach axis-gate** hardening (already in `pyproject.toml`; enforce in CI).

See the [ADRs](adr/) for architectural decisions behind each phase.
