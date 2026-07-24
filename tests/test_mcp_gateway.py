"""tests/test_mcp_gateway.py — McpGateway unit tests (ADR-029)."""

from __future__ import annotations

import asyncio

import pytest
from kernel.domain import Artifact
from kernel.events import EventBus, EventStore
from kernel.mcp_domain import McpTool
from kernel.mcp_gateway import McpGateway, McpGatewayError
from kernel.mcp_store import McpStore


async def _instant(_seconds: float) -> None:
    return None


class MockHttp:
    """Deterministic JSON-RPC mock: method -> result dict (or exception)."""

    def __init__(self, results: dict | None = None, fail_times: int = 0, exc: Exception | None = None):
        self.results = results or {}
        self.fail_times = fail_times
        self.exc = exc or ConnectionError("transient")
        self.calls: list[tuple[str, dict]] = []

    async def post(self, url: str, json: dict) -> dict:
        self.calls.append((url, json))
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.exc
        method = json["method"]
        if method not in self.results:
            return {"jsonrpc": "2.0", "id": json["id"], "error": {"code": -32601, "message": f"unknown method {method}"}}
        return {"jsonrpc": "2.0", "id": json["id"], "result": self.results[method]}


INIT_RESULT = {
    "protocolVersion": "2024-11-05",
    "serverInfo": {"name": "weather-mcp", "version": "1.2.0"},
    "capabilities": {"tools": {}, "resources": {}},
}
TOOLS_RESULT = {
    "tools": [
        {"name": "weather.fetch", "description": "Fetch weather", "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}}},
        {"name": "weather.forecast", "description": "Forecast", "inputSchema": {"type": "object"}},
    ]
}
CALL_RESULT = {"content": [{"type": "text", "text": "sunny"}], "isError": False}
READ_RESULT = {"contents": [{"uri": "res://cities", "mimeType": "text/plain", "text": "Moscow"}]}

URL = "http://mcp.local"


def _gateway(http=None, **kw) -> McpGateway:
    defaults = dict(
        event_bus=EventBus(),
        event_store=EventStore(),
        store=McpStore(),
        sleep=_instant,
        http_client=http or MockHttp({"initialize": INIT_RESULT, "tools/list": TOOLS_RESULT, "tools/call": CALL_RESULT, "resources/read": READ_RESULT}),
    )
    defaults.update(kw)
    return McpGateway(**defaults)


async def test_connect_success() -> None:
    gw = _gateway()
    session = await gw.connect(URL, auth_token="tok")
    assert session.status == "active"
    assert session.server_url == URL
    events = await gw._event_store.read_stream(session.session_id)
    assert any(e.type == "mcp.connected" for e in events)
    server = gw._store.get_server(URL)
    assert server is not None and server.name == "weather-mcp" and server.version == "1.2.0"


async def test_connect_failure_emits_mcp_error() -> None:
    http = MockHttp(results={})  # no "initialize" -> JSON-RPC error
    gw = _gateway(http=http)
    with pytest.raises(McpGatewayError):
        await gw.connect(URL)
    events = await gw._event_store.read_stream(URL)
    assert any(e.type == "mcp.error" and e.payload["error_type"] == "protocol" for e in events)


async def test_list_tools_parses_schema() -> None:
    gw = _gateway()
    tools = await gw.list_tools(URL)
    assert len(tools) == 2
    fetch = next(t for t in tools if t.name == "weather.fetch")
    assert fetch.input_schema["properties"]["city"]["type"] == "string"
    assert fetch.server_url == URL
    # cached in store
    assert gw._store.get_tool("weather.fetch", URL) is not None


async def test_call_tool_returns_artifact() -> None:
    gw = _gateway()
    await gw.connect(URL)
    artifact = await gw.call_tool(URL, "weather.fetch", {"city": "Moscow"})
    assert isinstance(artifact, Artifact)
    assert artifact.type == "mcp_tool_result"
    assert artifact.format == "json"
    assert artifact.content == CALL_RESULT


async def test_call_tool_emits_mcp_tool_called_on_bus() -> None:
    captured: list = []
    bus = EventBus()

    async def handler(event) -> None:
        captured.append(event)

    bus.subscribe("mcp.tool_called", handler)
    gw = _gateway(event_bus=bus)
    session = await gw.connect(URL)
    await gw.call_tool(URL, "weather.fetch", {"city": "Kyiv"})
    await asyncio.sleep(0)
    assert any(e.type == "mcp.tool_called" and e.aggregate_id == session.session_id for e in captured)
    evt = next(e for e in captured if e.type == "mcp.tool_called")
    assert evt.payload["tool_name"] == "weather.fetch"
    assert "arguments_hash" in evt.payload and "latency_ms" in evt.payload


async def test_read_resource_success() -> None:
    gw = _gateway()
    await gw.connect(URL)
    text = await gw.read_resource(URL, "res://cities")
    assert text == "Moscow"


async def test_read_resource_emits_mcp_resource_read() -> None:
    gw = _gateway()
    session = await gw.connect(URL)
    await gw.read_resource(URL, "res://cities")
    events = await gw._event_store.read_stream(session.session_id)
    reads = [e for e in events if e.type == "mcp.resource_read"]
    assert len(reads) == 1
    assert reads[0].payload["uri"] == "res://cities"
    assert reads[0].payload["size_bytes"] == len("Moscow")


async def test_close_session_emits_mcp_session_closed() -> None:
    gw = _gateway()
    session = await gw.connect(URL)
    await gw.close_session(session.session_id, reason="shutdown")
    assert gw.get_session(session.session_id).status == "closed"
    events = await gw._event_store.read_stream(session.session_id)
    closed = [e for e in events if e.type == "mcp.session_closed"]
    assert len(closed) == 1 and closed[0].payload["reason"] == "shutdown"


async def test_resolve_capability_exact_match() -> None:
    gw = _gateway()
    await gw.list_tools(URL)
    tool = gw.resolve_capability("mcp:weather.fetch")
    assert tool is not None and tool.name == "weather.fetch"
    # explicit server form
    tool2 = gw.resolve_capability(f"mcp:{URL}::weather.forecast")
    assert tool2 is not None and tool2.name == "weather.forecast"


async def test_resolve_capability_wildcard() -> None:
    gw = _gateway()
    await gw.list_tools(URL)
    tool = gw.resolve_capability("mcp:weather.*")
    assert tool is not None and tool.name.startswith("weather")
    assert gw.resolve_capability("mcp:nonexistent.*") is None


async def test_retry_on_transient_error() -> None:
    http = MockHttp({"initialize": INIT_RESULT}, fail_times=2)
    slept: list[float] = []

    async def recording_sleep(seconds: float) -> None:
        slept.append(seconds)

    gw = _gateway(http=http, sleep=recording_sleep)
    session = await gw.connect(URL)
    assert session.status == "active"
    assert len(slept) == 2  # two retries with backoff before success
    assert len(http.calls) == 3


async def test_retry_exhausted_raises_and_emits_error() -> None:
    http = MockHttp({"initialize": INIT_RESULT}, fail_times=99)
    gw = _gateway(http=http)
    with pytest.raises(McpGatewayError):
        await gw.connect(URL)
    events = await gw._event_store.read_stream(URL)
    assert any(e.type == "mcp.error" and e.payload["error_type"] == "transport" for e in events)


async def test_timeout_handling() -> None:
    http = MockHttp({"initialize": INIT_RESULT}, fail_times=1, exc=TimeoutError("slow"))
    gw = _gateway(http=http)
    with pytest.raises(McpGatewayError, match="timed out"):
        await gw.connect(URL)
    events = await gw._event_store.read_stream(URL)
    assert any(e.type == "mcp.error" and e.payload["error_type"] == "timeout" for e in events)


async def test_no_http_client_raises() -> None:
    gw = McpGateway()
    with pytest.raises(McpGatewayError, match="no http_client"):
        await gw.connect(URL)


async def test_discover_local_tools_from_store_fallback() -> None:
    store = McpStore()
    store.put_tool(McpTool(name="cached.tool", server_url=URL))
    gw = McpGateway(store=store)
    tools = gw.discover_local_tools()
    assert len(tools) == 1 and tools[0].name == "cached.tool"
    # no cache, no store -> []
    assert McpGateway().discover_local_tools() == []
