# ADR-029 — MCP Gateway / Protocol Adapter

**Status:** Accepted · **Version:** v2.15.0 · **Date:** 2026-07-25

## Context

The kernel is declared "MCP-native" in the README, but until now there was no
first-class integration with the Model Context Protocol on the *client* side:
the `mcp/` package only exposes the kernel *as* an MCP server (stdio / SSE /
Streamable HTTP transports). The kernel itself could not call external MCP
tools as capabilities, nor advertise remote MCP tools through the marketplace.

ADR-029 adds a **thin gateway** so that:

1. The kernel calls external MCP tools as first-class capabilities
   (`mcp:<server_url>::<tool>` or `mcp:<tool>` resolved via a cached tool map).
2. Discovered MCP tools surface in the `PluginMarketplace` catalog as virtual
   `PluginPackage`s (`source=MCP_SERVER`), making them visible in
   `list_available()`.
3. Everything is optionally wireable (`mcp=None` default) — zero regression on
   the 649-test baseline.

## Decision

### New modules (axis: `kernel.mcp_domain` + `kernel.events` + `kernel.domain.Artifact` only)

- **`kernel/mcp_domain.py`** — isolated ADR-local models (established pattern:
  `semantic_graph.py` / `marketplace_domain.py` / `observability_domain.py`):
  `McpServer`, `McpTool`, `McpResource`, `McpSession`.
- **`kernel/mcp_gateway.py`** — `McpGateway`: async, fully injectable
  (`event_bus`, `event_store`, `store`, `clock`, `sleep`, `http_client`,
  `metrics`). Speaks MCP **2024-11-05** as JSON-RPC 2.0 over an injected async
  `http_client.post(url, json) -> dict`. Operations: `connect` (initialize
  handshake), `list_tools` (tools/list, cached in store), `call_tool`
  (tools/call → `Artifact(type="mcp_tool_result")`), `read_resource`
  (resources/read), `close_session`, `discover_local_tools`,
  `resolve_capability` (exact + `prefix.*` wildcard). Bounded retry with
  injectable `sleep`, timeout via `asyncio.wait_for`, protocol errors emit
  `McpError` and surface as `Artifact(type="error")` from `call_tool`.
  Optional `metrics` (ADR-027 `ObservabilityEngine`, duck-typed) records
  `mcp.tool_latency_ms` / `mcp.tool_errors`.
- **`kernel/mcp_store.py`** — `McpStore`: SQLite tables `servers`, `tools`,
  `sessions`, `calls`; in-memory fallback with `db_path=None` (nullable
  connection initialized before the conditional — ADR-026 lesson); repo-reload
  on `db_path`.

### Events (`kernel/events.py`, namespaced `mcp.*`)

| Event | aggregate_id | payload |
|---|---|---|
| `McpConnected` | session_id | session_id, server_url, server_name, server_version |
| `McpToolCalled` | session_id | tool_name, arguments_hash, latency_ms |
| `McpResourceRead` | session_id | uri, size_bytes |
| `McpSessionClosed` | session_id | reason |
| `McpError` | server_url | error_type, message |

### Integration (all optional, `mcp=None` default)

- **`AgentRuntime(mcp=...)`** — `execute()` routes `mcp:*` capabilities through
  the gateway; **deterministic contract**: `mcp:*` with no gateway wired raises
  `RuntimeError("MCP gateway not wired")` (no silent fallback).
  `list_mcp_tools()` proxies to the gateway or returns `[]`.
- **`WorkflowEngine(mcp=...)`** — `execute_step` / `_run_step` route `mcp:*`
  steps through the gateway and record `context["mcp_latency_ms"]`. An `mcp:*`
  step with `mcp=None` emits `WorkflowStepFailed(reason="mcp_not_wired")` and
  fails the instance (ADR-028 guard-style honest failure).
- **`PluginMarketplace(mcp=...)`** — `discover_mcp_tools(source_url)` connects,
  lists tools, records `CatalogEntry` per tool and virtual `PluginPackage`s
  (`source=PluginSource.MCP_SERVER`, new enum member); `list_available()`
  augments its result with those virtual packages when the gateway is wired.

## Consequences

- Kernel can consume any MCP-2024-11-05 server over HTTP JSON-RPC with fully
  deterministic tests (mock `http_client`, injectable `sleep`/`clock`).
- 36 new tests (gateway 15, store 8, integration 13); 685 passed, 3 skipped;
  kernel coverage 92%; tach green. Baseline 649 tests pass unchanged.
- New per-file coverage: `mcp_domain.py` 100%, `mcp_gateway.py` 96%,
  `mcp_store.py` 96%.

## Honest limitations

- **MCP 2024-11-05 client-only** — the gateway does not make the kernel a
  bidirectional MCP peer; server-side exposure remains in `mcp/` (ADR-008).
- **JSON-RPC over HTTP only** — no stdio and no SSE streaming transport on the
  client path.
- **Request-response only** — no server-side streaming, progress notifications
  or sampling callbacks.
- **Basic auth / Bearer token** — token is attached as `_meta.authorization`;
  no OAuth 2.1 PKCE flow.
- **In-memory session cache + SQLite** — sessions are logical records, not
  persistent WebSocket/streaming connections; a restart loses live sessions
  (store keeps the audit trail).
- **Capability→MCP mapping is by name** (exact match + `prefix.*` wildcard),
  not semantic matching.
- `discover_mcp_tools` stores catalog entries via the existing
  `MarketplaceStore.put_catalog_entry` when possible; virtual MCP packages
  themselves live in the in-memory `_mcp_packages` map (not persisted — they
  are re-discoverable from the MCP server).

## Deferred

- SSE / Streamable HTTP client transport parity with `mcp/` server side.
- OAuth PKCE authorization flow.
- Exposing kernel capabilities as MCP tools *through the gateway* (currently
  handled by the separate `mcp/` server package).
- Semantic capability→tool matching (embedding-based).
