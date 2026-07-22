"""mcp/server_streamable.py — Streamable HTTP transport for MCP (ADR-008).

Bridges the existing stdio ``MCPServer`` JSON-RPC core to the **Streamable HTTP**
transport specified by MCP 2024-11-05. Unlike the SSE transport (where the POST
returns 202 and the response is pushed back over a separate GET stream), the
Streamable HTTP transport returns the JSON-RPC response **directly in the POST
body** (HTTP 200, ``application/json``). A separate ``GET`` endpoint provides a
server→client SSE stream for server-initiated notifications.

Endpoints:
    POST /mcp/v1/messages   client -> server (JSON-RPC, application/json)
    GET  /mcp/v1/events     server -> client (text/event-stream; notifications)

Sessions: identified by the ``Mcp-Session-Id`` HTTP header (not a query param).
A POST without a session id creates a new session and returns its id in the
response header. JSON-RPC batches (``requests: [...]``) are split, handled
individually, and aggregated into a response array.

Resumability (``Last-Event-ID`` replay) is a future concern — the header is
parsed and accepted, but backlog replay is not yet implemented (in-memory
session store only).

AXIS CONTRACT: depends on kernel (domain, registry, bus) + the existing
``mcp.server.MCPServer`` (owns the protocol). Stdlib ``http.server`` only —
no FastAPI/uvicorn, keeping the dependency surface minimal.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kernel.bus import EventBus
from kernel.domain import Tool
from kernel.registry import ToolRegistry

from mcp.server import MCPServer

logger = logging.getLogger("hermes.mcp.streamable")

SESSION_HEADER = "Mcp-Session-Id"


class StreamableHTTPSession:
    """One client session; holds the server->client SSE event queue."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        # server-initiated events (notifications) for the GET /events stream
        self.events: "queue.Queue[str | None]" = queue.Queue()
        self.created_at = time.time()

    def push_event(self, data: str) -> None:
        """Queue a server->client SSE event (e.g. tools/list_changed)."""
        self.events.put(data)

    def get_event(self, timeout: float | None = None) -> str | None:
        return self.events.get(timeout=timeout)

    def close(self) -> None:
        self.events.put(None)  # sentinel: stream ends


class MCPServerStreamable:
    """Streamable HTTP transport wrapper around ``MCPServer``."""

    def __init__(self, tool_registry: ToolRegistry, event_bus: EventBus) -> None:
        self._server = MCPServer(tool_registry, event_bus)
        self._sessions: dict[str, StreamableHTTPSession] = {}
        self._lock = threading.Lock()
        self._http: ThreadingHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host = "127.0.0.1"
        self._port = 0

    # -- delegate protocol config to the underlying server ---------------- #
    def set_handler(self, tool_name: str, handler) -> None:
        """Register a Python callable invoked by tools/call for ``tool_name``."""
        self._server.set_handler(tool_name, handler)

    # -- session registry ------------------------------------------------ #
    def _create_session(self) -> StreamableHTTPSession:
        sid = uuid.uuid4().hex
        s = StreamableHTTPSession(sid)
        with self._lock:
            self._sessions[sid] = s
        return s

    def _session(self, session_id: str | None) -> StreamableHTTPSession | None:
        if not session_id:
            return None
        with self._lock:
            return self._sessions.get(session_id)

    def _end_session(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is not None:
            s.close()

    # -- core dispatch (testable without a socket) ----------------------- #
    def dispatch(self, msg: dict) -> dict | None:
        """Run one JSON-RPC message on the server loop; return its response.

        Mirrors ``MCPServer._handle`` semantics: a notification (no ``id``)
        returns ``None``; a request returns its JSON-RPC result/error object.
        """
        if self._loop is None:
            logger.warning("dispatch before server start; dropping")
            return None
        future = asyncio.run_coroutine_threadsafe(self._server._handle(msg), self._loop)
        try:
            return future.result(timeout=10)
        except Exception:  # never crash the HTTP thread
            logger.exception("dispatch failed")
            return None

    # -- HTTP: POST /mcp/v1/messages ------------------------------------- #
    def _handle_post_messages(self, handler: "BaseHTTPRequestHandler") -> None:
        length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(length) if length else b"{}"
        session = self._session(handler.headers.get(SESSION_HEADER))

        if session is None:
            # new session: create + advertise id in the response header
            session = self._create_session()

        try:
            body = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._respond_json(handler, session, 400, {"error": "invalid JSON"})
            return

        # JSON-RPC batch: requests[] -> split, handle each, aggregate
        if isinstance(body, list):
            responses = [self.dispatch(m) for m in body if isinstance(m, dict)]
            # notifications yield None; keep only real responses in the array
            payload = [r for r in responses if r is not None]
            self._respond_json(handler, session, 200, payload)
            return

        response = self.dispatch(body)
        if response is None:
            # notification -> 202 Accepted, empty body
            self._respond(handler, session, 202, "application/json", b"")
        else:
            self._respond_json(handler, session, 200, response)

    # -- HTTP: GET /mcp/v1/events ---------------------------------------- #
    def _handle_get_events(self, handler: "BaseHTTPRequestHandler") -> None:
        session = self._session(handler.headers.get(SESSION_HEADER))
        if session is None:
            handler.send_response(404)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(b'{"error": "unknown or missing session"}')
            return
        # Last-Event-ID is parsed for future resumability; backlog replay is a
        # future concern, so we simply open a fresh stream from now on.
        _last_event_id = handler.headers.get("Last-Event-ID")

        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        # SSE keep-alive comment: confirms the stream is open (clients ignore it)
        handler.wfile.write(b": connected\n\n")
        handler.wfile.flush()
        try:
            while True:
                item = session.get_event(timeout=30)
                if item is None:  # closed
                    break
                handler.wfile.write(item.encode("utf-8"))
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            # GET stream closing does not end the session (POST may continue)
            with self._lock:
                if session.session_id in self._sessions:
                    # drain the queue so a later GET can reconnect cleanly
                    while not session.events.empty():
                        session.events.get_nowait()

    # -- response helpers ------------------------------------------------- #
    def _respond(
        self,
        handler: "BaseHTTPRequestHandler",
        session: StreamableHTTPSession,
        code: int,
        content_type: str,
        body: bytes,
    ) -> None:
        handler.send_response(code)
        handler.send_header("Content-Type", content_type)
        handler.send_header(SESSION_HEADER, session.session_id)
        handler.send_header("Access-Control-Allow-Origin", "*")
        handler.end_headers()
        handler.wfile.write(body)

    def _respond_json(
        self,
        handler: "BaseHTTPRequestHandler",
        session: StreamableHTTPSession,
        code: int,
        payload: Any,
    ) -> None:
        self._respond(
            handler,
            session,
            code,
            "application/json",
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        )

    # -- server lifecycle ------------------------------------------------ #
    def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Start the Streamable HTTP server (background thread) + own loop."""
        self._loop = asyncio.new_event_loop()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True)
        self._loop_thread.start()

        outer = self
        base = "/mcp/v1"

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                if self.path.split("?")[0] == f"{base}/messages":
                    outer._handle_post_messages(self)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0] == f"{base}/events":
                    outer._handle_get_events(self)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # silence default logging
                pass

        self._http = ThreadingHTTPServer((host, port), _Handler)
        self._host, self._port = self._http.server_address[:2]
        t = threading.Thread(target=self._http.serve_forever, daemon=True)
        t.start()
        logger.info("MCP Streamable HTTP on http://%s:%s%s", self._host, self._port, base)

    @property
    def address(self) -> tuple[str, int]:
        return (self._host, self._port)

    def stop(self) -> None:
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=2)
            self._loop.close()
            self._loop = None
        if self._http is not None:
            self._http.shutdown()
            self._http.server_close()
            self._http = None
            logger.info("MCP Streamable HTTP server stopped")
