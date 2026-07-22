"""mcp/server_sse.py — SSE transport for the MCP server (variant A of P4).

Bridges the existing stdio ``MCPServer`` JSON-RPC logic to the **SSE transport**
described by the MCP spec:

    GET  /sse                     -> opens text/event-stream, first event is
                                     `event: endpoint` carrying the POST URL
                                     (``/messages/?sessionId=...``)
    POST /messages/?sessionId=...  -> JSON-RPC request; the response is pushed
                                     back over that session's SSE stream

AXIS CONTRACT: depends on kernel (domain, registry, bus) + the existing
``mcp.server.MCPServer`` (which owns the protocol). This module is transport
only — no protocol logic duplicated.

No FastAPI / uvicorn: the HTTP layer uses stdlib ``http.server`` on a worker
thread; SSE pushes are scheduled onto the server's asyncio loop via
``run_coroutine_threadsafe``. Keeps dependencies minimal and the axis clean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from kernel.bus import EventBus
from kernel.domain import Tool
from kernel.registry import ToolRegistry

from mcp.server import MCPServer

logger = logging.getLogger("hermes.mcp.sse")


class SSESession:
    """One connected SSE client; holds the outgoing message queue."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.queue: "queue.Queue[str | None]" = queue.Queue()
        self.created_at = time.time()

    def put(self, data: str) -> None:
        self.queue.put(data)

    def get(self, timeout: float | None = None) -> str | None:
        return self.queue.get(timeout=timeout)

    def close(self) -> None:
        self.queue.put(None)  # sentinel: stream ends


class MCPServerSSE:
    """SSE-transport wrapper around ``MCPServer``."""

    def __init__(self, tool_registry: ToolRegistry, event_bus: EventBus) -> None:
        self._server = MCPServer(tool_registry, event_bus)
        self._sessions: dict[str, SSESession] = {}
        self._lock = threading.Lock()
        self._http: ThreadingHTTPServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._host = "127.0.0.1"
        self._port = 0
        self._counter = 0

    # -- delegate protocol config to the underlying server ---------------- #
    def set_handler(self, tool_name: str, handler) -> None:
        self._server.set_handler(tool_name, handler)

    # -- session registry ------------------------------------------------ #
    def _new_session_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"s{self._counter}"

    def _session(self, session_id: str) -> SSESession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def _create_session(self) -> SSESession:
        sid = self._new_session_id()
        s = SSESession(sid)
        with self._lock:
            self._sessions[sid] = s
        return s

    def _end_session(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.pop(session_id, None)
        if s is not None:
            s.close()

    # -- dispatch (testable without a socket) ---------------------------- #
    def dispatch(self, msg: dict, session: SSESession) -> None:
        """Handle one JSON-RPC message and push any response to the SSE stream.

        Runs the underlying async ``MCPServer._handle`` on the server loop and
        forwards the result (if any) as an SSE ``message`` event. Notifications
        (no id) produce no response, matching stdio behaviour.
        """
        if self._loop is None:
            logger.warning("dispatch before server start; dropping")
            return
        future = asyncio.run_coroutine_threadsafe(
            self._server._handle(msg), self._loop
        )
        try:
            response = future.result(timeout=10)
        except Exception:  # never crash the HTTP thread
            logger.exception("dispatch failed")
            return
        if response is not None:
            session.put(f"event: message\ndata: {json.dumps(response, ensure_ascii=False)}\n\n")

    # -- HTTP request handling (runs on worker threads) ------------------ #
    def _handle_get_sse(self, handler: "BaseHTTPRequestHandler") -> None:
        session = self._create_session()
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "keep-alive")
        handler.end_headers()
        # first event advertises the POST endpoint for this session
        endpoint = f"/messages/?sessionId={session.session_id}"
        handler.wfile.write(
            f"event: endpoint\ndata: {endpoint}\n\n".encode("utf-8")
        )
        handler.wfile.flush()
        try:
            while True:
                item = session.get(timeout=30)
                if item is None:  # closed
                    break
                handler.wfile.write(item.encode("utf-8"))
                handler.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            self._end_session(session.session_id)

    def _handle_post_messages(self, handler: "BaseHTTPRequestHandler") -> None:
        length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(length) if length else b"[]"
        parsed = urllib.parse.urlparse(handler.path)
        qs = urllib.parse.parse_qs(parsed.query)
        sid = (qs.get("sessionId") or [None])[0]
        session = self._session(sid) if sid else None
        handler.send_response(202)
        handler.send_header("Content-Type", "application/json")
        handler.end_headers()
        handler.wfile.write(b"{}")
        if session is None:
            logger.warning("POST to unknown/missing session %s", sid)
            return
        try:
            msg = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            logger.warning("invalid JSON-RPC body")
            return
        # JSON-RPC allows a batch (list); dispatch each
        if isinstance(msg, list):
            for m in msg:
                self.dispatch(m, session)
        else:
            self.dispatch(msg, session)

    # -- server lifecycle ------------------------------------------------ #
    def start(self, host: str = "127.0.0.1", port: int = 0) -> None:
        """Start the SSE HTTP server (background thread) + capture the loop."""
        # own event loop on a dedicated thread, so run_coroutine_threadsafe
        # (used by dispatch from HTTP worker threads) always has a live loop
        self._loop = asyncio.new_event_loop()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_forever()

        self._loop_thread = threading.Thread(target=_run_loop, daemon=True)
        self._loop_thread.start()

        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                if self.path.split("?")[0] == "/sse":
                    outer._handle_get_sse(self)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):  # noqa: N802
                if self.path.split("?")[0] == "/messages/":
                    outer._handle_post_messages(self)
                else:
                    self.send_response(404)
                    self.end_headers()

            def log_message(self, *args):  # silence default logging
                pass

        self._http = ThreadingHTTPServer((host, port), _Handler)
        self._host, self._port = self._http.server_address[:2]
        t = threading.Thread(target=self._http.serve_forever, daemon=True)
        t.start()
        logger.info("MCP SSE server on http://%s:%s/sse", self._host, self._port)

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
            logger.info("MCP SSE server stopped")
