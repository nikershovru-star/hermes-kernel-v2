"""tests/test_mcp.py — MCP server protocol, tools adapter, event logging."""

import asyncio
import json

import pytest

from kernel import domain, bus, registry
from mcp.server import MCPServer
from mcp.tools import MCPToolAdapter


@pytest.fixture
def harness() -> tuple[MCPServer, registry.ToolRegistry, bus.EventBus]:
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    srv = MCPServer(tr, tb)
    return srv, tr, tb


async def test_initialize_handshake(harness) -> None:
    srv, _, _ = harness
    resp = await srv._handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert resp["result"]["serverInfo"]["name"] == "hermes-kernel-v2"
    # notifications/initialized -> no response
    note = await srv._handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert note is None


async def test_tools_list_empty(harness) -> None:
    srv, _, _ = harness
    resp = await srv._handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp["result"]["tools"] == []


async def test_tools_list_returns_tools(harness) -> None:
    srv, tr, _ = harness
    await tr.register(domain.Tool(name="pdf", capability="hermes.fs.read",
                                   input_schema={"type": "object",
                                                 "properties": {"path": {"type": "string"}},
                                                 "required": ["path"]}))
    resp = await srv._handle({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
    tools = resp["result"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "pdf"
    assert tools[0]["inputSchema"]["required"] == ["path"]


async def test_tools_call_success(harness) -> None:
    srv, tr, _ = harness
    await tr.register(domain.Tool(name="echo", capability="hermes.echo", input_schema={}))

    def handler(args: dict) -> str:
        return f"got:{args.get('v')}"

    srv.set_handler("echo", handler)
    resp = await srv._handle(
        {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
         "params": {"name": "echo", "arguments": {"v": 42}}}
    )
    assert resp["result"]["content"][0]["text"] == "got:42"


async def test_tools_call_not_found(harness) -> None:
    srv, _, _ = harness
    resp = await srv._handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "ghost"}}
    )
    assert resp["error"]["code"] == -32602  # Invalid Params (tool not found)


async def test_tools_call_invalid_params(harness) -> None:
    srv, tr, _ = harness
    await tr.register(domain.Tool(
        name="need", capability="x",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
    ))
    resp = await srv._handle(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
         "params": {"name": "need", "arguments": {}}}  # missing required n
    )
    assert resp["error"]["code"] == -32602


async def test_event_logged_to_bus(harness) -> None:
    srv, tr, tb = harness
    await tr.register(domain.Tool(name="ev", capability="x", input_schema={}))

    def handler(args: dict) -> str:
        return "ok"

    srv.set_handler("ev", handler)
    seen: list[str] = []

    async def _cap(e):
        seen.append(e.type)

    tb.subscribe("mcp.tool.call", _cap)
    await srv._handle(
        {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "ev", "arguments": {}}}
    )
    await asyncio.sleep(0.05)
    assert "mcp.tool.call" in seen


async def test_server_start_stop() -> None:
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    await tr.register(domain.Tool(name="ping", capability="x", input_schema={}))
    srv = MCPServer(tr, tb)

    srv.set_handler("ping", lambda a: "pong")

    reader = asyncio.StreamReader()
    writer = _CollectWriter()

    # seed an initialize request, then EOF after a tiny delay
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    body = json.dumps(init).encode("utf-8")
    framed = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
    reader.feed_data(framed)

    async def _eof_later():
        await asyncio.sleep(0.1)
        reader.feed_eof()

    asyncio.ensure_future(_eof_later())

    task = asyncio.ensure_future(srv.start(reader=reader, writer=writer))
    await asyncio.sleep(0.05)
    assert srv._running is True
    await task  # loop exits on EOF

    out = writer.getvalue().decode("utf-8")
    assert "protocolVersion" in out  # server processed + wrote response
    assert srv._running is False


class _CollectWriter:
    def __init__(self) -> None:
        self._buf = bytearray()

    def write(self, data: bytes) -> None:
        self._buf += data

    async def drain(self) -> None:
        pass

    def close(self) -> None:
        pass

    def getvalue(self) -> bytes:
        return bytes(self._buf)
