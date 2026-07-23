# ADR-012: MCP Streamable HTTP hardening — TTL eviction & protocol version

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review
- **Supersedes / Related:** ADR-008 (Streamable HTTP + durable sessions)

## Context

ADR-008 introduced durable SSE event logs: every server→client frame is
persisted as a `McpSessionEvent` (workspace `mcp:<session_id>`) into a
`PersistenceRegistry`. With a file-backed store this survives disconnects and
server restarts (Last-Event-ID replay).

Two gaps remained:

1. **Unbounded growth.** `McpSessionEvent` rows accumulate forever; a long-lived
   session's file grows without limit. A time-to-live + background eviction is
   required.
2. **No version negotiation.** A client and server may implement different MCP
   protocol revisions. There is no mechanism to detect / advertise the version.

## Decision

### 1. Session TTL / eviction
`MCPServerStreamable.__init__` gains `session_ttl: int = 86400` (seconds) and
`evict_interval: int = 3600` (seconds). On `start()` the server schedules a
background asyncio task (on its own loop) that, every `evict_interval`, calls
`async _evict_expired()`.

`_evict_expired()`:
- early-returns if `persistence is None` or `session_ttl <= 0` (eviction opt-out);
- reads `created_at` (UTC ISO-8601) of every persisted `McpSessionEvent` per
  known session workspace, parses it with `datetime.fromisoformat` (timezone-aware),
  and deletes rows older than `now - session_ttl`;
- is best-effort: any DB error is logged, never raised (the live stream must
  not be affected). `delete` is called per expired row via `PersistenceRegistry.delete`.

Rationale: eviction is **workspace-scoped** (ADR-007) — we scan only known
session workspaces, never a global table. TTL default 24h matches typical
long-polling/SSE retention expectations.

### 2. Mcp-Protocol-Version negotiation
- New header constant `Mcp-Protocol-Version` (`PROTOCOL_VERSION_HEADER`).
- `MCPServerStreamable.__init__` gains `protocol_version: str = "2024-11-05"`
  (`DEFAULT_PROTOCOL_VERSION`).
- Both `POST /mcp/v1/messages` and `GET /mcp/v1/events` call
  `_check_protocol_version(handler)` first. Rules:
  - **header absent** → legacy client, accepted (no negotiation).
  - **header == server version** → accepted; server echoes the version in the
    response headers (`_respond` / `_handle_get_events`).
  - **header != server version** → `426 Upgrade Required`, response body
    `{"error": "unsupported protocol version", "supported": "<server>"}`,
    and the `Mcp-Protocol-Version` header advertises the supported version.
- The server advertises its version on **every** response (POST 200/202/400,
  GET 200) so even legacy clients learn the server's revision.

Rationale: 426 is the standard "Upgrade Required" status for protocol
negotiation; echoing the version keeps the contract explicit and debuggable.

## Axis / constraints

- `mcp/server_streamable.py` depends only on `kernel.*` (domain, registry, bus,
  persistence) + `mcp.server` — axis unchanged, `tach check` stays green.
- No new optional dependencies; stdlib `datetime` only.
- Backward compatible: all new params have safe defaults; existing behaviour
  (durable sessions, replay) is preserved. `session_ttl=0` disables eviction.

## Consequences

- File-backed MCP event logs no longer grow unbounded (bounded by TTL).
- Incompatible clients fail fast with a clear 426 + supported-version hint.
- 6 new tests in `tests/test_mcp_streamable.py` (TTL eviction ×2, protocol
  negotiation ×3, background scheduling ×1); module coverage 86%.

## Alternatives considered

- *Truncate by count* (keep last N events): simpler but loses time semantics;
  rejected in favour of TTL (time-based retention is what "durable log" implies).
- *Evict inside `push_event`* (synchronous trim on write): risks blocking the
  SSE path on DB I/O; rejected — eviction is a separate background task.
- *Version fallback to oldest supported* instead of 426: more complex, less
  explicit; rejected — 426 + advertised version is the RESTful norm.
