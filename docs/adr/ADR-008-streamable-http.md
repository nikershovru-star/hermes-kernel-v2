# ADR-008 — Streamable HTTP Transport

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md), [ADR-003](ADR-003-mcp-integration.md), [ADR-007](ADR-007-workspace-isolation.md)

## Context

MCP 2024-11-05 defines two HTTP transports. We already shipped the **SSE
transport** in [ADR-003 addendum / `mcp/server_sse.py`](#) (variant A): `GET
/sse` opens a stream and the client POSTs to a `?sessionId=` query-param
endpoint, receiving the JSON-RPC response *pushed back* over the SSE stream
(HTTP 202 on POST). This works but forces a separate GET connection just to
receive responses.

The **Streamable HTTP transport** is the modern MCP shape: the client POSTs
JSON-RPC and receives the response **directly in the POST body** (HTTP 200,
`application/json`); a separate `GET` endpoint carries server→client
notifications over SSE. Sessions are identified by the `Mcp-Session-Id` HTTP
header (not a query param), and JSON-RPC batches (`requests: [...]`) are
aggregated into a response array.

## Decision

Implement `mcp/server_streamable.py` as a **transport layer over the existing
`MCPServer` JSON-RPC core** (no protocol duplication), mirroring the proven
lifecycle of `mcp/server_sse.py`:

- **Own asyncio loop on a dedicated thread**; HTTP handlers run on
  `ThreadingHTTPServer` worker threads and dispatch via
  `asyncio.run_coroutine_threadsafe(self._server._handle(msg), self._loop)`.
- **`POST /mcp/v1/messages`** — reads `Mcp-Session-Id`; creates a session (UUID)
  if absent and returns its id in the response header; dispatches the JSON-RPC
  message on the server loop and returns the response **in the POST body**
  (200). A batch `list` is split, each element handled, and non-`None`
  responses aggregated into the array. A notification (no `id`) → `202`.
- **`GET /mcp/v1/events`** — requires a valid `Mcp-Session-Id` (else `404`);
  opens a `text/event-stream` for server→client notifications, emitting an SSE
  keep-alive comment (`: connected`) on connect. `Last-Event-ID` is parsed for
  future resumability; backlog replay is **not** implemented yet (in-memory
  session store).
- **Session store:** in-memory `dict[str, StreamableHTTPSession]` (single
  process). Durable storage via `PersistenceRegistry` is a future concern.
- **Zero new dependencies:** stdlib `http.server` only — consistent with the
  axis contract (ADR-001) and the SSE transport choice.

### Why POST-returns-response (vs SSE)
The Streamable HTTP transport is simpler for clients: one POST → one response,
no second connection to correlate. The `GET /events` channel is reserved for
genuine server push (e.g. `notifications/tools/list_changed`), which the kernel
can emit via `MCPServer.notify_tools_changed` through the session event queue.

## Status Quo (implemented)

| Endpoint | Method | Behaviour |
|----------|--------|-----------|
| `/mcp/v1/messages` | POST | JSON-RPC in, response in body; `Mcp-Session-Id` header; batch aggregate |
| `/mcp/v1/events` | GET | SSE stream for server→client notifications; `Last-Event-ID` accepted |

Verified in `tests/test_mcp_streamable.py` (7 tests, 82% cov): session creation,
reuse, batch aggregation, notification → 202, unknown-session → 404, stream open.

## Consequences

- **Good:** modern MCP transport; no query-param session coupling; batch-native;
  reuses 100% of the JSON-RPC core; no new deps.
- **Bad / cost:** in-memory sessions (no cross-restart durability, no resumable
  replay yet); one TCP connection per GET /events stream.
- **Future:** durable sessions (PersistenceRegistry), `Last-Event-ID` backlog
  replay, optional `Mcp-Protocol-Version` negotiation header.

## Rejected Alternatives

- **FastAPI / uvicorn:** would add two heavy deps; the stdlib `http.server`
  shape already satisfies the spec and keeps the axis clean (ADR-001).
- **Reuse SSE module verbatim:** the response-return semantics differ enough
  (body vs pushed stream) that a dedicated transport layer is clearer than
  bolting both onto one class.
