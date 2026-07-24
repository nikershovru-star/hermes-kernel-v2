"""tests/test_mcp_store.py — McpStore persistence tests (ADR-029)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from kernel.mcp_domain import McpServer, McpSession, McpTool
from kernel.mcp_store import McpStore

URL = "http://mcp.local"


def _sqlite_store(tmp_path) -> McpStore:
    return McpStore(db_path=str(tmp_path / "mcp.db"))


def test_sqlite_roundtrip_server_tool_session_call(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    store.put_server(McpServer(url=URL, name="w", version="1.0", auth_token="t", capabilities=["tools"]))
    store.put_tool(McpTool(name="a.b", description="d", input_schema={"type": "object"}, server_url=URL))
    session = McpSession(session_id="s1", server_url=URL, status="active")
    store.put_session(session)
    store.put_call("c1", "s1", "a.b", "hash", 12.5)

    server = store.get_server(URL)
    assert server.name == "w" and server.auth_token == "t" and server.capabilities == ["tools"]
    tool = store.get_tool("a.b", URL)
    assert tool.description == "d" and tool.input_schema == {"type": "object"}
    got = store.get_session("s1")
    assert got.status == "active" and got.server_url == URL
    calls = store.list_calls("s1")
    assert len(calls) == 1 and calls[0]["latency_ms"] == 12.5
    store.close()


def test_list_tools_filtered_by_server_url(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    store.put_tool(McpTool(name="t1", server_url=URL))
    store.put_tool(McpTool(name="t2", server_url=URL))
    store.put_tool(McpTool(name="t3", server_url="http://other"))
    assert {t.name for t in store.list_tools(URL)} == {"t1", "t2"}
    assert len(store.list_tools()) == 3
    store.close()


def test_in_memory_fallback() -> None:
    store = McpStore(db_path=None)
    assert store._conn is None  # no AttributeError (ADR-026 pitfall guard)
    store.put_server(McpServer(url=URL))
    store.put_tool(McpTool(name="x", server_url=URL))
    store.put_session(McpSession(session_id="s1", server_url=URL))
    store.put_call("c1", "s1", "x")
    assert store.get_server(URL) is not None
    assert store.get_tool("x", URL) is not None
    assert store.get_session("s1") is not None
    assert len(store.list_calls("s1")) == 1
    assert store.get_server("http://missing") is None
    assert store.get_tool("nope", URL) is None
    assert store.get_session("nope") is None


def test_repo_reload_on_db_path(tmp_path) -> None:
    db = str(tmp_path / "mcp.db")
    store = McpStore(db_path=db)
    store.put_server(McpServer(url=URL, name="persisted"))
    store.put_tool(McpTool(name="t", server_url=URL))
    store.close()
    reloaded = McpStore(db_path=db)
    assert reloaded.get_server(URL).name == "persisted"
    assert reloaded.get_tool("t", URL) is not None
    reloaded.close()


def test_get_session_after_close(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    session = McpSession(session_id="s1", server_url=URL, status="active")
    store.put_session(session)
    session.status = "closed"
    store.put_session(session)  # upsert
    assert store.get_session("s1").status == "closed"
    store.close()


def test_list_calls_with_since_until(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    t0 = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)
    for i in range(3):
        store.put_call(f"c{i}", "s1", "tool", timestamp=t0 + timedelta(hours=i))
    mid = store.list_calls("s1", since=t0 + timedelta(minutes=30), until=t0 + timedelta(minutes=90))
    assert [c["call_id"] for c in mid] == ["c1"]
    assert len(store.list_calls("s1", since=t0)) == 3
    assert len(store.list_calls("s1", until=t0)) == 1
    store.close()


def test_call_latency_aggregation(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    for i, ms in enumerate([10.0, 20.0, 30.0]):
        store.put_call(f"c{i}", "s1", "tool", latency_ms=ms)
    store.put_call("cx", "s2", "tool", latency_ms=100.0)
    calls = store.list_calls("s1")
    latencies = [c["latency_ms"] for c in calls]
    assert sum(latencies) / len(latencies) == 20.0
    assert max(latencies) == 30.0
    store.close()


def test_schema_json_serialization(tmp_path) -> None:
    store = _sqlite_store(tmp_path)
    schema = {"type": "object", "properties": {"q": {"type": "string", "enum": ["a", "b"]}}, "required": ["q"]}
    store.put_tool(McpTool(name="complex", server_url=URL, input_schema=schema))
    got = store.get_tool("complex", URL)
    assert got.input_schema == schema  # deep-equal after JSON roundtrip
    store.close()
