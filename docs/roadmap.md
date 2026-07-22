# Hermes Kernel v2 — Roadmap

_Last updated: 2026-07-22 · **113 passed, 0 failed, 90% total coverage**_

| Phase | Название | Статус | Этапы | Gate |
|-------|----------|--------|-------|------|
| P0 | Kernel Core | ✅ | domain, bus, registry, plugins | 62 passed, 94% |
| P1 | Runtime + Capability | ✅ | capability, executor, workspace, integration | 108 passed, 95% |
| P2 | Knowledge Pipeline | ⏳ | scanner, parser, chunker, embedding, graph | — |
| P3 | MCP + SDK | ✅ | client, sdk | 113 passed, 90% |
| P4 | MCP Server | ✅* | server, tools | 25 MCP tests green |
| P5 | Multi-tenancy | ⏳ | auth, RBAC, isolation | — |

> **\* P4 note:** the MCP *server* + `MCPToolAdapter` were implemented alongside
> the P3 client (see [ADR-003](adr/ADR-003-mcp-integration.md)). `mcp/server.py`
> (81%) and `mcp/tools.py` (97%) are covered by `test_mcp.py` (8) and
> `test_mcp_transport.py` (9). Remaining: SSE transport (stdio-only today).

## Current test surface (113 tests)

| Suite | Tests | Suite | Tests |
|-------|-------|-------|-------|
| test_domain | 27 | test_registry | 9 |
| test_loader_errors | 12 | test_mcp_transport | 9 |
| test_bus | 8 | test_capability | 8 |
| test_mcp | 8 | test_mcp_client | 8 |
| test_executor | 6 | test_loader | 5 |
| test_sdk | 5 | test_workspace | 5 |
| test_integration | 3 | | |

## Module coverage snapshot

| Module | Cov | Module | Cov |
|--------|-----|--------|-----|
| kernel/domain.py | 100% | kernel/capability.py | 97% |
| kernel/workspace.py | 97% | kernel/executor.py | 92% |
| kernel/registry.py | 92% | kernel/bus.py | 90% |
| mcp/tools.py | 97% | mcp/client.py | 81% |
| mcp/server.py | 81% | plugins/loader.py | 97% |
| plugins/sdk (pkg) | 95% | plugins/base.py | 88% |

## Next up

- **P2 — Knowledge Pipeline:** `scanner → parser → chunker → embedding → graph`,
  driven by the Event bus (`document.scanned → document.parsed → chunk.created →
  chunk.embedded`). This is the largest remaining vertical.
- **P4 hardening:** SSE transport for the MCP server.
- **P5 — Multi-tenancy:** auth, RBAC, workspace isolation (single-tenant today
  per ADR-001).

See the [ADRs](adr/) for architectural decisions behind each phase.
