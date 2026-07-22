"""tests/test_mcp_transport.py — framing, notifications, error codes, adapter edges.

Supplements test_mcp.py to push mcp coverage >= 85%.
"""

import asyncio
import json

import pytest

from kernel import domain, bus, registry
from mcp.server import MCPServer
from mcp.tools import MCPToolAdapter


@pytest.fixture
def harness():
    tb = bus.EventBus()
    tr = registry.ToolRegistry()
    return MCPServer(tr, tb), tr, tb


# --- transport framing round-trip ----------------------------------------- #
async def test_framing_roundtrip(harness) -> None:
    srv, _, _ = harness
    reader = asyncio.StreamReader()
    writer = _CollectWriter()

    # craft a framed message and feed it to the reader
    msg = {"jsonrpc": "2.0", "id": 1, "method": "initialize"}
    body = json.dumps(msg).encode("utf-8")
    framed = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8") + body
    reader.feed_data(framed)
    reader.feed_eof()
    srv._reader = reader  # unit-inject reader (start() does this in prod)
    srv._writer = writer

    parsed = await srv._read_message()
    assert parsed == msg

    # _send writes a framed response
    await srv._send({"jsonrpc": "2.0", "id": 1, "result": {}})
    out = writer.getvalue()
    assert out.startswith(b"Content-Length: ")
    assert b'"id": 1' in out


# --- notify_tools_changed ------------------------------------------------ #
async def test_notify_tools_changed_writes_notification(harness) -> None:
    srv, _, _ = harness
    srv._writer = _CollectWriter()
    await srv.notify_tools_changed()
    out = srv._writer.getvalue().decode("utf-8")
    assert "notifications/tools/list_changed" in out


# --- error codes ---------------------------------------------------------- #
async def test_invalid_request(harness) -> None:
    srv, _, _ = harness
    resp = await srv._handle({"not": "jsonrpc"})
    assert resp["error"]["code"] == -32600


async def test_method_not_found(harness) -> None:
    srv, _, _ = harness
    resp = await srv._handle({"jsonrpc": "2.0", "id": 9, "method": "bogus/method"})
    assert resp["error"]["code"] == -32601


async def test_server_error_when_no_handler(harness) -> None:
    srv, tr, _ = harness
    await tr.register(domain.Tool(name="orphan", capability="x", input_schema={}))
    # no set_handler -> _invoke raises RuntimeError -> -32000
    resp = await srv._handle(
        {"jsonrpc": "2.0", "id": 10, "method": "tools/call", "params": {"name": "orphan", "arguments": {}}}
    )
    assert resp["error"]["code"] == -32000


async def test_tools_call_async_handler(harness) -> None:
    srv, tr, _ = harness
    await tr.register(domain.Tool(name="async_echo", capability="x", input_schema={}))

    async def handler(args: dict) -> str:
        await asyncio.sleep(0)
        return "async-ok"

    srv.set_handler("async_echo", handler)
    resp = await srv._handle(
        {"jsonrpc": "2.0", "id": 11, "method": "tools/call", "params": {"name": "async_echo", "arguments": {}}}
    )
    assert resp["result"]["content"][0]["text"] == "async-ok"


# --- adapter edges -------------------------------------------------------- #
def test_adapter_from_mcp_call() -> None:
    tool, args = MCPToolAdapter.from_mcp_call("foo", {"a": 1})
    assert tool.name == "foo"
    assert args == {"a": 1}


def test_adapter_validate_additional_properties_false() -> None:
    tool = domain.Tool(
        name="strict", capability="x",
        input_schema={"type": "object", "properties": {"a": {"type": "string"}},
                       "additionalProperties": False},
    )
    assert MCPToolAdapter.validate_arguments(tool, {"a": "ok"}) is True
    assert MCPToolAdapter.validate_arguments(tool, {"unknown": 1}) is False


def test_adapter_validate_wrong_type() -> None:
    tool = domain.Tool(
        name="typed", capability="x",
        input_schema={"type": "object", "properties": {"n": {"type": "integer"}}},
    )
    assert MCPToolAdapter.validate_arguments(tool, {"n": "notint"}) is False


# --- helpers -------------------------------------------------------------- #
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
