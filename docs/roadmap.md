# Hermes Kernel v2 — Roadmap

_Last updated: 2026-07-22 · **140 passed, 0 failed, 90.5% total coverage**_

| Phase | Название | Статус | Этапы | Gate |
|-------|----------|--------|-------|------|
| P0 | Kernel Core | ✅ | domain, bus, registry, plugins | 62 passed, 94% |
| P1 | Runtime + Capability | ✅ | capability, executor, workspace, integration | 108 passed, 95% |
| P2 | Knowledge Pipeline | ✅ | scanner, parser, chunker, embedder, graph + e2e | 140 passed, 90.5% |
| P3 | MCP + SDK | ✅ | client, sdk | 113 passed, 90% |
| P4 | MCP Server | ✅* | server, tools | 25 MCP tests green |
| P5 | Multi-tenancy | ⏳ | auth, RBAC, isolation | — |

> **\* P4 note:** the MCP *server* + `MCPToolAdapter` were implemented alongside
> the P3 client (see [ADR-003](adr/ADR-003-mcp-integration.md)). Remaining: SSE
> transport (stdio-only today).

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

## Current test surface (140 tests)

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
| test_embedder | 4 | test_integration | 3 |
| test_integration_p2 | 1 | | |

## Module coverage snapshot

| Module | Cov | Module | Cov |
|--------|-----|--------|-----|
| kernel/domain.py | 100% | kernel/capability.py | 97% |
| kernel/workspace.py | 97% | kernel/scanner.py | 96% |
| kernel/graph.py | 96% | kernel/chunker.py | 94% |
| kernel/executor.py | 92% | kernel/registry.py | 92% |
| kernel/bus.py | 90% | kernel/parser.py | 84% |
| kernel/embedder.py | 84% | mcp/tools.py | 97% |
| mcp/client.py | 81% | mcp/server.py | 81% |
| plugins/loader.py | 97% | plugins/sdk (pkg) | 95% |

## Next up

- **P5 — Multi-tenancy:** auth (users, roles), RBAC, workspace isolation
  enforcement (single-tenant today per ADR-001).
- **P4 hardening:** SSE transport for the MCP server.
- **Persistence:** `KnowledgeRetrievalService` — vector DB behind the in-memory
  graph (future).

See the [ADRs](adr/) for architectural decisions behind each phase.
