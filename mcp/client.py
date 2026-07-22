"""mcp/client.py — MCP stdio client (JSON-RPC 2.0) for Hermes Kernel v2.

Connects to external MCP servers over stdio, performs the initialize
handshake, lists/calls tools, and bridges imported tools into the kernel's
ToolRegistry via MCPToolAdapter. Emits lifecycle Events on the shared EventBus.

AXIS CONTRACT: imports kernel.domain (Event, Tool) and kernel.registry
(ToolRegistry). No external MCP library — raw stdio JSON-RPC per MCP spec.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional

from kernel.bus import EventBus
from kernel.domain import Event, Tool
from kernel.registry import ToolRegistry

logger = logging.getLogger(__name__)

EVENT_CONNECTED = "mcp.client.connected"
EVENT_DISCONNECTED = "mcp.client.disconnected"
EVENT_ERROR = "mcp.client.error"
EVENT_TOOL_CALLED = "mcp.tool.called"


class MCPToolAdapter:
    """Converts MCP tool descriptors (dict) into kernel Tool entities."""

    def __init__(self, capability_namespace: str = "mcp") -> None:
        self.capability_namespace = capability_namespace

    def to_kernel_tool(self, raw: dict[str, Any]) -> Optional[Tool]:
        try:
            name = raw["name"]
            schema = raw.get("inputSchema") or raw.get("input_schema") or {}
            return Tool(
                name=name,
                capability=f"{self.capability_namespace}.{name}",
                input_schema=schema,
            )
        except (KeyError, TypeError) as exc:
            logger.warning("Skipping invalid MCP tool %r: %s", raw.get("name"), exc)
            return None


class MCPClientError(RuntimeError):
    """Transport / protocol failure talking to the MCP server."""


class MCPClient:
    """Async MCP stdio client. One subprocess per client instance."""

    def __init__(
        self,
        bus: EventBus,
        tool_registry: ToolRegistry,
        adapter: Optional[MCPToolAdapter] = None,
    ) -> None:
        self._bus = bus
        self._tools = tool_registry
        self._adapter = adapter or MCPToolAdapter()
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._req_id = 0
        self._pending: dict[Any, asyncio.Future] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server_info: Optional[dict] = None
        self._capabilities: Optional[dict] = None
        self._connected = False
        self._shutdown = False

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def server_info(self) -> Optional[dict]:
        return self._server_info

    async def connect(self, command: list[str], env: Optional[dict] = None) -> None:
        if self._connected:
            raise MCPClientError("already connected")
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        self._proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            env=run_env,
        )
        self._reader = self._proc.stdout
        self._writer = self._proc.stdin
        self._loop = asyncio.get_running_loop()
        self._connected = True
        self._shutdown = False
        self._read_task = asyncio.create_task(self._read_loop())
        self._bus.publish(
            Event(type=EVENT_CONNECTED, payload={"pid": self._proc.pid}, source="mcp.client")
        )

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF: process gone
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("MCP: cannot decode %r", line)
                    continue
                rid = msg.get("id")
                if rid is None:
                    continue  # notification, no response expected
                fut = self._pending.pop(rid, None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(MCPClientError(str(msg["error"])))
                    else:
                        fut.set_result(msg)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP read loop crashed")
            self._emit_error(str(exc))
        finally:
            if not self._shutdown:
                self._emit_error("connection closed unexpectedly")

    def _emit_error(self, error: str) -> None:
        self._connected = False
        try:
            self._bus.publish(
                Event(type=EVENT_ERROR, payload={"error": error}, source="mcp.client")
            )
        except Exception:  # noqa: BLE001
            pass

    async def _rpc(self, method: str, params: Optional[dict] = None) -> dict:
        if not self._connected or self._writer is None:
            raise MCPClientError("not connected")
        assert self._loop is not None
        self._req_id += 1
        rid = self._req_id
        fut: asyncio.Future = self._loop.create_future()
        self._pending[rid] = fut
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()
        return await fut

    async def _notify(self, method: str, params: Optional[dict] = None) -> None:
        if self._writer is None:
            raise MCPClientError("not connected")
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._writer.write((json.dumps(payload) + "\n").encode())
        await self._writer.drain()

    async def initialize(self) -> dict:
        resp = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "clientInfo": {"name": "hermes-kernel", "version": "2.0.0"},
                "capabilities": {},
            },
        )
        result = resp.get("result", {})
        self._server_info = result.get("serverInfo")
        self._capabilities = result.get("capabilities")
        await self._notify("notifications/initialized")
        return {"serverInfo": self._server_info, "capabilities": self._capabilities}

    async def tools_list(self) -> list[Tool]:
        """List MCP tools, convert + register them in the kernel ToolRegistry."""
        resp = await self._rpc("tools/list", {})
        raw_tools = resp.get("result", {}).get("tools", [])
        imported: list[Tool] = []
        for raw in raw_tools:
            tool = self._adapter.to_kernel_tool(raw)
            if tool is None:
                continue
            try:
                await self._tools.register(tool)
                imported.append(tool)
            except ValueError as exc:
                logger.warning("MCP tool %s not registered: %s", tool.name, exc)
        return imported

    async def tools_call(self, name: str, arguments: dict) -> dict:
        resp = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        self._bus.publish(
            Event(type=EVENT_TOOL_CALLED, payload={"name": name, "arguments": arguments},
                  source="mcp.client")
        )
        if "error" in resp:
            return {"error": resp["error"]}
        return {"result": resp.get("result")}

    async def disconnect(self) -> None:
        """Graceful shutdown: SIGTERM, wait (timeout -> SIGKILL), publish event."""
        self._shutdown = True
        if self._proc is not None and self._proc.returncode is None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._proc.kill()
                try:
                    await self._proc.wait()
                except ProcessLookupError:
                    pass
        if self._read_task is not None:
            self._read_task.cancel()
        self._connected = False
        self._bus.publish(
            Event(type=EVENT_DISCONNECTED, payload={}, source="mcp.client")
        )
