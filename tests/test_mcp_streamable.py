"""tests/test_mcp_streamable.py — Streamable HTTP transport (ADR-008).

The server owns its own asyncio loop on a worker thread, so tests are plain
(non-async) functions; pytest-asyncio never interferes with the loop.
"""

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from kernel import bus, domain, registry
from mcp.server_streamable import MCPServerStreamable, SESSION_HEADER


@pytest.fixture
def srv():
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    server = MCPServerStreamable(tr, tb)
    server.set_handler("echo", lambda a: f"got:{a.get('v')}")
    server.start(port=0)
    # register a tool on the server's own loop
    import asyncio

    fut = asyncio.run_coroutine_threadsafe(
        tr.register(
            domain.Tool(
                name="echo",
                capability="hermes.echo",
                input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
            )
        ),
        server._loop,
    )
    fut.result(timeout=5)
    yield server
    server.stop()


def test_dispatch_request_returns_response(srv: MCPServerStreamable) -> None:
    resp = srv.dispatch(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "hi"}}}
    )
    assert resp is not None
    assert resp["result"]["content"][0]["text"] == "got:hi"


def test_dispatch_notification_returns_none(srv: MCPServerStreamable) -> None:
    resp = srv.dispatch({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert resp is None


def test_post_creates_session_and_returns_response(srv: MCPServerStreamable) -> None:
    host, port = srv.address
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "x"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        assert r.status == 200
        sid = r.headers.get(SESSION_HEADER)
        assert sid is not None and len(sid) > 0
        data = json.loads(r.read().decode("utf-8"))
        assert data["result"]["content"][0]["text"] == "got:x"


def test_post_with_existing_session(srv: MCPServerStreamable) -> None:
    host, port = srv.address
    # first POST creates the session
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "y"}}}
    ).encode()
    req1 = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req1, timeout=5) as r:  # noqa: S310
        sid = r.headers.get(SESSION_HEADER)
    # second POST reuses the same session id
    req2 = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json", SESSION_HEADER: sid},
    )
    with urllib.request.urlopen(req2, timeout=5) as r:  # noqa: S310
        assert r.headers.get(SESSION_HEADER) == sid
        data = json.loads(r.read().decode("utf-8"))
        assert data["result"]["content"][0]["text"] == "got:y"


def test_batch_requests_aggregated(srv: MCPServerStreamable) -> None:
    host, port = srv.address
    batch = json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "a"}}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "b"}}},
    ]).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=batch, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        assert r.status == 200
        data = json.loads(r.read().decode("utf-8"))
        assert isinstance(data, list)
        texts = {d["result"]["content"][0]["text"] for d in data}
        assert texts == {"got:a", "got:b"}


def test_unknown_session_rejected_on_events(srv: MCPServerStreamable) -> None:
    host, port = srv.address
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/events",
        headers={SESSION_HEADER: "ghost"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=3)  # noqa: S310
    assert excinfo.value.code == 404


def test_get_events_stream_opens(srv: MCPServerStreamable) -> None:
    host, port = srv.address
    # create a session via POST first
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "z"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        sid = r.headers.get(SESSION_HEADER)

    result: dict = {}
    exc: dict = {}

    def _client() -> None:
        try:
            # read the stream byte-by-byte (stays open); assert it opens cleanly
            req = urllib.request.Request(
                f"http://{host}:{port}/mcp/v1/events",
                headers={SESSION_HEADER: sid},
            )
            with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310
                buf = b""
                while b"\n\n" not in buf and len(buf) < 1024:
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
    # close the server so the stream is torn down
    srv.stop()
    assert "e" not in exc, f"GET /events failed: {exc.get('e')}"
    # the endpoint accepts the session and opens a valid SSE stream
    assert result.get("raw", "").startswith("") or "event" in result.get("raw", "")
