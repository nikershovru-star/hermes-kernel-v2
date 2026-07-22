# ADR-003 — MCP Integration

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md)

## Context

MCP (Model Context Protocol) is the kernel's **first external interface**
(per ADR-001 strategic decision). The kernel must both *consume* external MCP
servers (import their tools) and *expose* its own tools to MCP clients. The
protocol wire format (JSON-RPC 2.0 over stdio) is small and fully specified, so
we implement it directly rather than pulling in an external MCP SDK — keeping
the dependency graph clean and the axis contract intact.

## Decision

### 1. MCP Client (`mcp/client.py`) — consume external servers

Async stdio JSON-RPC 2.0 client:

| Method | Behaviour |
|--------|-----------|
| `connect(command, env)` | spawn subprocess + background read-loop; emits `mcp.client.connected` |
| `initialize()` | handshake → `serverInfo` + `capabilities`; sends `notifications/initialized` |
| `tools_list()` | fetch remote tools → adapt → register into `ToolRegistry` (namespaced `mcp.<name>`) |
| `tools_call(name, args)` | RPC call; JSON-RPC protocol error → `MCPClientError`; emits `mcp.tool.called` |
| `disconnect()` | SIGTERM → wait(timeout) → SIGKILL; emits `mcp.client.disconnected` |

Connection faults (EOF / read-loop exception) emit `mcp.client.error` rather than
crashing the caller.

### 2. MCPToolAdapter (`mcp/tools.py`) — kernel Tool ↔ MCP Tool

Pure adapter, depends on `kernel.domain` only (no I/O, no bus):

- `to_kernel_tool(mcp_tool)` — MCP Tool JSON → `kernel.domain.Tool`
  (`inputSchema` → `input_schema`, capability namespaced).
- `to_mcp_tool(tool)` — `kernel.domain.Tool` → MCP Tool JSON.
- argument validation against the tool's JSON Schema.

### 3. MCP Server (`mcp/server.py`) — expose kernel tools

`MCPServer(tool_registry, event_bus)` — stdio JSON-RPC 2.0, protocol
`2024-11-05`. Bridges `ToolRegistry` + `EventBus`:

- Handles `initialize`, `tools/list`, `tools/call`, `notifications/initialized`.
- JSON-RPC error codes: `-32600` invalid request, `-32601` method not found,
  `-32602` invalid params, `-32000` server error.
- `start(reader, writer)` accepts injectable asyncio streams — in production
  wired to process stdio, in tests fed in-memory pipes (fully exercisable
  without a real TTY, important on Windows).

## Status of components

| Component | File | Status | Coverage | Tests |
|-----------|------|--------|----------|-------|
| MCP Client | `mcp/client.py` | ✅ implemented | 81% | `test_mcp_client.py` (8) |
| MCPToolAdapter | `mcp/tools.py` | ✅ implemented | 97% | covered via client/server |
| MCP Server | `mcp/server.py` | ✅ implemented | 81% | `test_mcp.py` (8), `test_mcp_transport.py` (9) |

> **Note on scope drift (honest record):** the original P3 plan scoped only the
> MCP *client*; the server was slated for a later P4. In practice the server and
> adapter were implemented alongside the client (25 MCP tests total, all green),
> so P4's "MCP Server" line in the roadmap is effectively **already delivered**.
> Remaining server work is transport hardening (SSE is a `# TODO`; only stdio is
> wired) — tracked as a follow-up, not a blocker.

## Consequences

**Positive**

- Kernel is bidirectionally MCP-capable (consume + expose) with zero external
  MCP dependencies.
- Adapter isolation (`mcp/tools.py`, 97%) keeps the domain unaware of MCP wire
  details.
- Stream injection makes the server testable without a TTY (critical on Windows).

**Negative / trade-offs**

- Hand-rolled JSON-RPC means we own protocol-version compatibility
  (`2024-11-05`); a spec bump requires manual updates.
- SSE transport not implemented — stdio only for now.

## Related

- [ADR-001 — Kernel Architecture](ADR-001-kernel-architecture.md)
- [ADR-002 — Plugin System](ADR-002-plugin-system.md)
- [Roadmap](../roadmap.md)
