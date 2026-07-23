"""tests/test_mcp_streamable.py — Streamable HTTP transport (ADR-008).

The server owns its own asyncio loop on a worker thread, so tests are plain
(non-async) functions; pytest-asyncio never interferes with the loop.
"""

import json
import threading
import time
import urllib.error
import urllib.request
import asyncio
from pathlib import Path

import pytest
from unittest.mock import patch

from kernel import bus, domain, registry
from kernel.domain import McpSessionEvent
from kernel.persistence import PersistenceRegistry
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


# --------------------------------------------------------------------------- #
# Durable sessions: persistence + Last-Event-ID replay (ADR-008 resumability)
# --------------------------------------------------------------------------- #
def test_push_event_persists_to_registry(tmp_path: Path) -> None:
    """push_event stores each SSE frame in the PersistenceRegistry."""
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    persist = PersistenceRegistry(db_path=str(tmp_path / "mcp.db"))
    server = MCPServerStreamable(tr, tb, persistence=persist)
    server.start(port=0)
    sid = "sess-persist-1"
    s = server._create_session()
    s.session_id = sid
    s.set_loop(server._loop)
    seq1 = s.push_event("event: ping\ndata: a\n")
    seq2 = s.push_event("event: ping\ndata: b\n")
    seq3 = s.push_event("event: ping\ndata: c\n")
    server.stop()
    import asyncio

    # frames landed in the store, workspace-isolated per session
    rows = asyncio.run(
        persist.list(workspace_id=f"mcp:{sid}", entity_type="McpSessionEvent")
    )
    assert len(rows) == 3
    seqs = sorted(r.seq for r in rows)
    assert seqs == [1, 2, 3]
    assert seq1 == 1 and seq2 == 2 and seq3 == 3


def test_get_events_replays_backlog_on_last_event_id(tmp_path: Path) -> None:
    """GET /events with Last-Event-ID=N replays frames with seq > N."""
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    persist = PersistenceRegistry(db_path=str(tmp_path / "mcp.db"))
    server = MCPServerStreamable(tr, tb, persistence=persist)
    server.start(port=0)
    host, port = server.address
    # create a session via POST so it is registered + loop-bound
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "seed"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        sid = r.headers.get(SESSION_HEADER)
    s = server._session(sid)
    # push 3 server->client events
    s.push_event("event: x\ndata: 1\n")
    s.push_event("event: x\ndata: 2\n")
    s.push_event("event: x\ndata: 3\n")

    result: dict = {}
    exc: dict = {}

    def _client() -> None:
        try:
            req = urllib.request.Request(
                f"http://{host}:{port}/mcp/v1/events",
                headers={SESSION_HEADER: sid, "Last-Event-ID": "1"},
            )
            with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310
                buf = b""
                # read until we have seen the last replayed frame (seq 3),
                # then let the test tear the stream down via server.stop()
                while b"data: 3" not in buf:
                    chunk = r.read(1)
                    if not chunk:
                        break
                    buf += chunk
                result["raw"] = buf.decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            # connection abort on server.stop() is expected; keep what we got
            if "raw" not in result:
                result["raw"] = ""
            exc["e"] = str(e)

    t = threading.Thread(target=_client, daemon=True)
    t.start()
    deadline = time.time() + 3
    while time.time() < deadline and "raw" not in result and "e" not in exc:
        time.sleep(0.05)
    server.stop()
    assert "e" not in exc, f"replay GET failed: {exc.get('e')}"
    # replayed frames are seq 2 and 3 only (seq 1 excluded by Last-Event-ID)
    raw = result.get("raw", "")
    assert "data: 2" in raw and "data: 3" in raw
    assert "data: 1" not in raw


def test_durable_session_survives_server_restart(tmp_path: Path) -> None:
    """With a file-backed store, events persist across server stop/start."""
    db = str(tmp_path / "mcp.db")
    sid = "sess-restart-1"

    # --- first server instance: push + persist --- #
    tb1 = bus.EventBus()
    tr1 = registry.ToolRegistry()
    p1 = PersistenceRegistry(db_path=db)
    s1 = MCPServerStreamable(tr1, tb1, persistence=p1)
    s1.start(port=0)
    sess = s1._create_session()
    sess.session_id = sid
    sess.set_loop(s1._loop)
    sess.push_event("event: y\ndata: persist-me\n")
    s1.stop()
    import asyncio
    asyncio.run(p1.close())

    # --- second server instance, same DB: replay from scratch --- #
    tb2 = bus.EventBus()
    tr2 = registry.ToolRegistry()
    p2 = PersistenceRegistry(db_path=db)
    s2 = MCPServerStreamable(tr2, tb2, persistence=p2)
    s2.start(port=0)
    host, port = s2.address
    # register a dummy session id with the same persistence workspace
    sess2 = s2._create_session()
    sess2.session_id = sid
    sess2.set_loop(s2._loop)
    rows = asyncio.run(
        p2.list(workspace_id=f"mcp:{sid}", entity_type="McpSessionEvent")
    )
    assert len(rows) == 1
    assert "persist-me" in rows[0].sse_data
    s2.stop()
    asyncio.run(p2.close())


# --------------------------------------------------------------------------- #
# MCP Streamable HTTP hardening: TTL eviction + protocol version (v1.2.0)
# --------------------------------------------------------------------------- #
def test_evict_expired_removes_old_events(tmp_path: Path) -> None:
    """_evict_expired() deletes McpSessionEvent older than session_ttl."""
    db = str(tmp_path / "mcp.db")
    sid = "sess-evict-1"
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    p = PersistenceRegistry(db_path=db)
    srv = MCPServerStreamable(tr, tb, persistence=p, session_ttl=10, evict_interval=9999)
    srv.start(port=0)
    # register a session under a deterministic id so eviction scans its
    # persistence workspace (mcp:<sid>).
    sess = srv._create_session()
    with srv._lock:
        srv._sessions.pop(sess.session_id, None)
        sess.session_id = sid
        srv._sessions[sid] = sess
    sess.set_loop(srv._loop)
    # two events created "now"; we advance the clock by 100s so both fall
    # outside the 10s TTL and get evicted.
    for seq in (1, 2):
        asyncio.run(
            p.save(
                McpSessionEvent(
                    session_id=sid, seq=seq,
                    sse_data=f"id: {seq}\ndata: x\n", workspace_id=f"mcp:{sid}",
                )
            )
        )
    real_now = time.time()
    with patch.object(time, "time", return_value=real_now + 100):
        asyncio.run(srv._evict_expired())
    rows = asyncio.run(p.list(workspace_id=f"mcp:{sid}", entity_type="McpSessionEvent"))
    srv.stop()
    asyncio.run(p.close())
    assert len(rows) == 0  # both evicted (older than TTL once clock advanced)


def test_evict_expired_disabled_when_ttl_zero(tmp_path: Path) -> None:
    """With session_ttl=0 eviction is a no-op (events retained)."""
    db = str(tmp_path / "mcp.db")
    sid = "sess-evict-2"
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    p = PersistenceRegistry(db_path=db)
    srv = MCPServerStreamable(tr, tb, persistence=p, session_ttl=0)
    srv.start(port=0)
    sess = srv._create_session()
    sess.session_id = sid
    sess.set_loop(srv._loop)
    asyncio.run(
        p.save(
            McpSessionEvent(
                session_id=sid, seq=1, sse_data="id: 1\ndata: x\n",
                workspace_id=f"mcp:{sid}",
            )
        )
    )
    asyncio.run(srv._evict_expired())
    rows = asyncio.run(p.list(workspace_id=f"mcp:{sid}", entity_type="McpSessionEvent"))
    srv.stop()
    asyncio.run(p.close())
    assert len(rows) == 1


def test_protocol_version_negotiation_ok(tmp_path: Path) -> None:
    """Client sending a matching Mcp-Protocol-Version gets it echoed."""
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    srv = MCPServerStreamable(tr, tb, protocol_version="2024-11-05")
    srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
    srv.start(port=0)
    fut = asyncio.run_coroutine_threadsafe(
        tr.register(
            domain.Tool(
                name="echo", capability="hermes.echo",
                input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
            )
        ),
        srv._loop,
    )
    fut.result(timeout=5)
    host, port = srv.address
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "pv"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Mcp-Protocol-Version": "2024-11-05"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        assert r.headers.get("Mcp-Protocol-Version") == "2024-11-05"
        data = json.loads(r.read().decode("utf-8"))
        assert data["result"]["content"][0]["text"] == "got:pv"
    srv.stop()


def test_protocol_version_mismatch_returns_426(tmp_path: Path) -> None:
    """An incompatible Mcp-Protocol-Version yields 426 Upgrade Required."""
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    srv = MCPServerStreamable(tr, tb, protocol_version="2024-11-05")
    srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
    srv.start(port=0)
    fut = asyncio.run_coroutine_threadsafe(
        tr.register(
            domain.Tool(
                name="echo", capability="hermes.echo",
                input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
            )
        ),
        srv._loop,
    )
    fut.result(timeout=5)
    host, port = srv.address
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "x"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Mcp-Protocol-Version": "1999-01-01"},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(req, timeout=5)  # noqa: S310
    assert excinfo.value.code == 426
    srv.stop()


def test_protocol_version_absent_legacy_client_ok(tmp_path: Path) -> None:
    """A client with no Mcp-Protocol-Version header is accepted (legacy)."""
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    srv = MCPServerStreamable(tr, tb, protocol_version="2024-11-05")
    srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
    srv.start(port=0)
    fut = asyncio.run_coroutine_threadsafe(
        tr.register(
            domain.Tool(
                name="echo", capability="hermes.echo",
                input_schema={"type": "object", "properties": {"v": {"type": "string"}}},
            )
        ),
        srv._loop,
    )
    fut.result(timeout=5)
    host, port = srv.address
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": "legacy"}}}
    ).encode()
    req = urllib.request.Request(
        f"http://{host}:{port}/mcp/v1/messages",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=5) as r:  # noqa: S310
        data = json.loads(r.read().decode("utf-8"))
        assert data["result"]["content"][0]["text"] == "got:legacy"
        # server still advertises its version on every response
        assert r.headers.get("Mcp-Protocol-Version") == "2024-11-05"
    srv.stop()


def test_eviction_runs_in_background(tmp_path: Path) -> None:
    """start() schedules the eviction task when persistence + TTL are set."""
    db = str(tmp_path / "mcp.db")
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    p = PersistenceRegistry(db_path=db)
    srv = MCPServerStreamable(tr, tb, persistence=p, session_ttl=10, evict_interval=9999)
    srv.start(port=0)
    assert srv._evict_task is not None
    assert not srv._evict_task.done()
    srv.stop()
    asyncio.run(p.close())
