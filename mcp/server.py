"""mcp/server.py — minimal MCP server (stdio, JSON-RPC 2.0, protocol 2024-11-05).

AXIS CONTRACT: depends on kernel (domain, registry, bus). No external MCP lib —
the wire protocol is small and fully specified by the task, so we implement it
directly to keep dependencies minimal and the axis clean.

Transport: stdio with `Content-Length` framing. SSE is a # TODO (not implemented).

Error codes (JSON-RPC):
  -32600 Invalid Request | -32601 Method Not Found | -32602 Invalid Params
  -32000 Server Error

Design note: `start()` accepts optional `reader`/`writer` (asyncio streams). In
production they are wired to process stdio; in tests we inject in-memory pipes,
so the message loop is fully exercisable without a real TTY (important on Windows).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Any, Callable

from kernel.bus import EventBus
from kernel.domain import Event, Tool
from kernel.registry import ToolRegistry

from mcp.tools import MCPToolAdapter

logger = logging.getLogger("hermes.mcp.server")

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "hermes-kernel-v2", "version": "0.1.0"}

E_INVALID_REQUEST = -32600
E_METHOD_NOT_FOUND = -32601
E_INVALID_PARAMS = -32602
E_SERVER_ERROR = -32000

ToolHandler = Callable[[dict], Any]


class MCPServer:
    """stdio MCP server bridging ToolRegistry + EventBus."""

    def __init__(self, tool_registry: ToolRegistry, event_bus: EventBus) -> None:
        self._tools = tool_registry
        self._bus = event_bus
        self._adapter = MCPToolAdapter()
        self._handlers: dict[str, ToolHandler] = {}
        self._running = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: "asyncio.StreamWriter | _PipeWriter | None" = None

    # -- JSON-RPC plumbing ------------------------------------------------ #
    @staticmethod
    def _rpc_error(code: int, message: str, msg_id: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _rpc_result(result: Any, msg_id: Any) -> dict:
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}

    # -- method handlers -------------------------------------------------- #
    async def _handle(self, msg: dict) -> dict | None:
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            return self._rpc_error(E_INVALID_REQUEST, "not a JSON-RPC 2.0 object", msg.get("id"))
        method = msg.get("method")
        msg_id = msg.get("id")
        params = msg.get("params") or {}

        # notifications need no response
        if msg_id is None:
            if method == "notifications/initialized":
                logger.info("client initialized")
            return None

        if method == "initialize":
            return self._rpc_result(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {}},
                    "serverInfo": SERVER_INFO,
                },
                msg_id,
            )
        if method == "tools/list":
            tools = await self._tools.list()
            return self._rpc_result(
                {"tools": [self._adapter.to_mcp_schema(t) for t in tools]}, msg_id
            )
        if method == "tools/call":
            return await self._handle_call(params, msg_id)
        return self._rpc_error(E_METHOD_NOT_FOUND, f"method not found: {method}", msg_id)

    async def _handle_call(self, params: dict, msg_id: Any) -> dict:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not name:
            return self._rpc_error(E_INVALID_PARAMS, "missing 'name'", msg_id)
        tool = await self._tools.get_by_name(name)
        if tool is None:
            return self._rpc_error(E_INVALID_PARAMS, f"tool not found: {name}", msg_id)
        if not self._adapter.validate_arguments(tool, arguments):
            return self._rpc_error(E_INVALID_PARAMS, "arguments failed schema validation", msg_id)
        try:
            result = await self._invoke(tool, arguments)
            self._bus.publish(
                Event(type="mcp.tool.call", source="mcp.server",
                      payload={"name": name, "args": arguments, "result": result})
            )
            return self._rpc_result({"content": [{"type": "text", "text": str(result)}]}, msg_id)
        except Exception as exc:  # never crash the loop
            self._bus.publish(
                Event(type="mcp.tool.call", source="mcp.server",
                      payload={"name": name, "args": arguments, "error": str(exc)})
            )
            return self._rpc_error(E_SERVER_ERROR, f"tool invocation failed: {exc}", msg_id)

    async def _invoke(self, tool: Tool, arguments: dict) -> Any:
        handler = self._handlers.get(tool.name)
        if handler is None:
            raise RuntimeError(f"no handler registered for tool '{tool.name}'")
        out = handler(arguments)
        if asyncio.iscoroutine(out):
            out = await out
        return out

    def set_handler(self, tool_name: str, handler: ToolHandler) -> None:
        """Register a Python callable invoked by tools/call for `tool_name`."""
        self._handlers[tool_name] = handler

    # -- tools/list_changed (optional notification) ----------------------- #
    async def notify_tools_changed(self) -> None:
        if self._writer is not None:
            await self._send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})

    # -- stdio transport -------------------------------------------------- #
    async def _send(self, obj: dict) -> None:
        if self._writer is None:
            return
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._writer.write(f"Content-Length: {len(data)}\r\n\r\n".encode("utf-8") + data)
        await self._writer.drain()

    async def _read_message(self) -> dict | None:
        assert self._reader is not None
        headers: dict[str, str] = {}
        while True:
            line = await self._reader.readline()
            if line in (b"", b"\r\n"):
                break
            if b":" in line:
                k, _, v = line.decode("utf-8").partition(":")
                headers[k.strip().lower()] = v.strip()
        if "content-length" not in headers:
            return None
        n = int(headers["content-length"])
        body = await self._reader.readexactly(n)
        return json.loads(body.decode("utf-8"))

    async def start(
        self,
        reader: asyncio.StreamReader | None = None,
        writer: "asyncio.StreamWriter | _PipeWriter | None" = None,
    ) -> None:
        """Begin the read/process/write loop. If reader/writer omitted, wire to stdio."""
        self._running = True
        if reader is not None and writer is not None:
            self._reader, self._writer = reader, writer
        else:
            self._reader, self._writer = _wire_stdio()
        logger.info("MCP server started")
        while self._running:
            try:
                msg = await self._read_message()
            except Exception:
                logger.exception("read error")
                continue
            if msg is None:
                if self._reader.at_eof():
                    self._running = False
                    break
                continue
            response = await self._handle(msg)
            if response is not None:
                await self._send(response)

    async def stop(self) -> None:
        self._running = False
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        logger.info("MCP server stopped")


# --- stdio wiring (Windows-safe via executor for stdin) ------------------ #
class _PipeWriter:
    """Async writer over process stdout (buffered, flushed)."""

    def __init__(self) -> None:
        self._buf = sys.stdout.buffer

    def write(self, data: bytes) -> None:
        self._buf.write(data)

    async def drain(self) -> None:
        self._buf.flush()

    def close(self) -> None:
        self._buf.flush()


async def _wire_stdio() -> tuple[asyncio.StreamReader, _PipeWriter]:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()

    # Windows-safe: read stdin bytes in an executor, feed the StreamReader
    async def _feed() -> None:
        while True:
            chunk = await loop.run_in_executor(None, sys.stdin.buffer.read, 1)
            if not chunk:
                break
            reader.feed_data(chunk)
        reader.feed_eof()

    asyncio.ensure_future(_feed())
    return reader, _PipeWriter()
