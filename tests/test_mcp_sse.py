"""tests/test_mcp_sse.py — SSE transport over the MCP server (variant A).

The SSE server owns its own asyncio loop on a worker thread, so tests are
plain (non-async) functions — pytest-asyncio never interferes with the loop.
"""

import asyncio
import threading
import time
import urllib.request

import pytest

from kernel import bus, domain, registry
from mcp.server_sse import MCPServerSSE, SSESession


@pytest.fixture
def sse_server():
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    srv = MCPServerSSE(tr, tb)
    srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
    # register a tool on the server's own loop (ToolRegistry.register is async)
    srv.start(port=0)
    fut = asyncio.run_coroutine_threadsafe(
        tr.register(
            domain.Tool(
                name="echo",
                capability="hermes.echo",
                input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
            )
        ),
        srv._loop,
    )
    fut.result(timeout=5)
    yield srv
    srv.stop()


def test_dispatch_pushes_response_to_session(sse_server: MCPServerSSE) -> None:
    sse_server.start(port=0)
    try:
        session = SSESession("test-1")
        sse_server._sessions["test-1"] = session
        sse_server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": "echo", "arguments": {"v": "hi"}}},
            session,
        )
        item = session.get(timeout=5)
        assert item is not None
        assert "event: message" in item
        assert '"got:hi"' in item
    finally:
        sse_server.stop()


def test_dispatch_notification_no_response(sse_server: MCPServerSSE) -> None:
    sse_server.start(port=0)
    try:
        session = SSESession("test-2")
        sse_server._sessions["test-2"] = session
        sse_server.dispatch(
            {"jsonrpc": "2.0", "method": "notifications/initialized"}, session
        )
        # notification has no id -> nothing pushed to the stream
        assert session.queue.empty()
    finally:
        sse_server.stop()


def test_start_stop_lifecycle(sse_server: MCPServerSSE) -> None:
    # fixture already started the server (to register a tool); verify it serves
    assert sse_server._http is not None
    assert sse_server._http.socket is not None  # bound to a port
    host, port = sse_server.address
    assert port > 0
    sse_server.stop()
    assert sse_server._http is None


def test_sse_endpoint_event_reachable(sse_server: MCPServerSSE) -> None:
    """GET /sse opens a stream and emits the `endpoint` event with sessionId."""
    sse_server.start(port=0)
    host, port = sse_server.address

    result: dict = {}
    exc: dict = {}

    def _client() -> None:
        try:
            # read the first SSE event block byte-by-byte (stream stays open)
            import socket as _sock

            with urllib.request.urlopen(
                f"http://{host}:{port}/sse", timeout=3
            ) as r:
                buf = b""
                while b"\n\n" not in buf and len(buf) < 4096:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk
                result["raw"] = buf.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            exc["e"] = str(e)

    t = threading.Thread(target=_client, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and "raw" not in result and "e" not in exc:
        time.sleep(0.05)
    sse_server.stop()

    assert "e" not in exc, f"SSE GET failed: {exc.get('e')}"
    raw = result.get("raw", "")
    assert "event: endpoint" in raw
    assert "/messages/?sessionId=" in raw


def test_post_unknown_session_accepted(sse_server: MCPServerSSE) -> None:
    """POST to a missing session returns 202 and does not crash the server."""
    sse_server.start(port=0)
    host, port = sse_server.address
    try:
        req = urllib.request.Request(
            f"http://{host}:{port}/messages/?sessionId=ghost",
            data=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}',
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310
            assert r.status == 202
    finally:
        sse_server.stop()
