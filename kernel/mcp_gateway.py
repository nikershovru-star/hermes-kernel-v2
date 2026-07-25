"""kernel/mcp_gateway.py — thin MCP client gateway (ADR-029).

Speaks MCP (2024-11-05 dialect) as JSON-RPC 2.0 over an injected async HTTP
``post(url, json) -> dict`` callable. The kernel can call remote MCP tools as
first-class capabilities and wrap results in ``Artifact``.

Axis contract: imports ONLY ``kernel.mcp_domain`` + ``kernel.events`` +
``kernel.domain`` (Artifact). Never imports agent / workflow / marketplace —
those wire the gateway optionally in the reverse direction.

All time / IO is injectable (``clock`` / ``sleep`` / ``http_client``) so tests
are fully deterministic.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from typing import Any, Awaitable, Callable

from kernel.config_domain import ConfigScope
from kernel.domain import Artifact
from kernel.events import (
    EventBus,
    EventStore,
    McpConnected,
    McpError,
    McpResourceRead,
    McpSessionClosed,
    McpToolCalled,
)
from kernel.mcp_domain import McpResource, McpServer, McpSession, McpTool
from kernel.mcp_store import McpStore
from kernel.resilience_domain import CircuitBreakerOpenError, RetryExhaustedError

MCP_PROTOCOL_VERSION = "2024-11-05"


class McpGatewayError(RuntimeError):
    """Raised when a JSON-RPC exchange with an MCP server fails."""


async def _default_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class McpGateway:
    """Async, fully-injectable MCP protocol adapter (client-only)."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        store: McpStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
        http_client: Any | None = None,
        metrics: Any | None = None,
        max_retries: int = 2,
        retry_backoff_seconds: float = 0.5,
        timeout_seconds: float = 30.0,
        vault: Any | None = None,
        resilience: Any | None = None,
    ) -> None:
        self._bus = event_bus
        self._event_store = event_store
        self._store = store
        self._clock = clock
        self._sleep = sleep
        self._http = http_client
        self._metrics = metrics  # optional ObservabilityEngine (ADR-027), duck-typed
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff_seconds
        self._timeout = timeout_seconds
        self._vault = vault  # ADR-030: optional ConfigVault for auth_token resolution
        self._resilience = resilience  # ADR-031: optional ResilienceEngine (circuit + retry)
        self._sessions: dict[str, McpSession] = {}  # session_id -> session
        self._by_server: dict[str, str] = {}  # server_url -> active session_id
        self._tools: dict[str, list[McpTool]] = {}  # server_url -> cached tools
        self._auth: dict[str, str] = {}  # server_url -> bearer token
        self._auth_source: dict[str, str] = {}  # server_url -> "explicit" | "vault" | "none"

    # -- JSON-RPC plumbing -------------------------------------------------- #
    async def _rpc(self, server_url: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """One JSON-RPC 2.0 request with bounded retry on transient errors.

        Retries up to ``max_retries`` times with linear backoff via the
        injectable ``sleep``. A response missing ``result`` (or carrying
        ``error``) raises ``McpGatewayError`` and emits ``McpError``.
        """
        if self._http is None:
            raise McpGatewayError("MCP gateway has no http_client wired")
        request = {
            "jsonrpc": "2.0",
            "id": uuid.uuid4().hex,
            "method": method,
            "params": dict(params),
        }
        token = self._auth.get(server_url)
        if token is not None:
            request["params"]["_meta"] = {"authorization": f"Bearer {token}"}
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                deadline_task = self._http.post(server_url, json=request)
                response = await asyncio.wait_for(deadline_task, timeout=self._timeout)
                break
            except (asyncio.TimeoutError, TimeoutError) as exc:
                await self._emit(McpError(server_url, "timeout", f"{method} timed out after {self._timeout}s"))
                raise McpGatewayError(f"MCP request '{method}' to {server_url} timed out") from exc
            except Exception as exc:  # noqa: BLE001 - transient transport error
                last_exc = exc
                if attempt < self._max_retries:
                    await self._sleep(self._retry_backoff * (attempt + 1))
                    continue
                await self._emit(McpError(server_url, "transport", str(exc)))
                raise McpGatewayError(f"MCP request '{method}' to {server_url} failed: {exc}") from exc
        else:  # pragma: no cover - loop always breaks or raises
            raise McpGatewayError(str(last_exc))
        if not isinstance(response, dict) or "result" not in response:
            error = (response or {}).get("error", {}) if isinstance(response, dict) else {}
            message = error.get("message", "invalid JSON-RPC response (no result)")
            await self._emit(McpError(server_url, "protocol", message))
            raise McpGatewayError(f"MCP '{method}' error from {server_url}: {message}")
        return response["result"]

    # -- lifecycle ------------------------------------------------------------ #
    async def connect(self, server_url: str, auth_token: str | None = None) -> McpSession:
        """JSON-RPC ``initialize`` handshake. Emits ``McpConnected``.

        ADR-030: when ``auth_token`` is None and a ``ConfigVault`` is wired, the
        gateway tries to resolve ``mcp:{server_url}:auth_token`` from the vault
        (scope=MCP_SERVER, scope_id=server_url). If found it is used as the
        bearer token; if not, the connection proceeds without auth (deterministic
        — no raise). The resolved source is tracked for audit context.
        """
        source = "none"
        if auth_token is not None:
            self._auth[server_url] = auth_token
            source = "explicit"
        elif self._vault is not None:
            try:
                resolved = await self._vault.resolve_secret(
                    f"mcp:{server_url}:auth_token",
                    scope=ConfigScope.MCP_SERVER,
                    scope_id=server_url,
                    accessor=f"mcp_gateway:{server_url}",
                )
            except (KeyError, RuntimeError):
                resolved = None
            if resolved is not None:
                self._auth[server_url] = resolved
                auth_token = resolved
                source = "vault"
        self._auth_source[server_url] = source
        result = await self._rpc(
            server_url,
            "initialize",
            {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "clientInfo": {"name": "hermes-kernel", "version": "2.15.0"},
                "capabilities": {"tools": {}, "resources": {}},
            },
        )
        info = result.get("serverInfo", {})
        server = McpServer(
            url=server_url,
            name=info.get("name", ""),
            version=info.get("version", ""),
            auth_token=auth_token,
            capabilities=sorted(result.get("capabilities", {}).keys()),
        )
        session = McpSession(session_id=uuid.uuid4().hex, server_url=server_url, status="active")
        self._sessions[session.session_id] = session
        self._by_server[server_url] = session.session_id
        if self._store is not None:
            self._store.put_server(server)
            self._store.put_session(session)
        await self._emit(McpConnected(session.session_id, server_url, server.name, server.version))
        return session

    async def close_session(self, session_id: str, reason: str = "explicit_close") -> None:
        """Mark a session closed and emit ``McpSessionClosed``."""
        session = self._sessions.get(session_id)
        if session is not None:
            session.status = "closed"
            self._by_server.pop(session.server_url, None)
            if self._store is not None:
                self._store.put_session(session)
        await self._emit(McpSessionClosed(session_id, reason))

    # -- tools ------------------------------------------------------------------ #
    async def list_tools(self, server_url: str) -> list[McpTool]:
        """JSON-RPC ``tools/list``; caches results in memory + store."""
        result = await self._rpc(server_url, "tools/list", {})
        tools = [
            McpTool(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_url=server_url,
            )
            for t in result.get("tools", [])
        ]
        self._tools[server_url] = tools
        if self._store is not None:
            for tool in tools:
                self._store.put_tool(tool)
        return tools

    async def call_tool(self, server_url: str, tool_name: str, arguments: dict[str, Any]) -> Artifact:
        """JSON-RPC ``tools/call`` → ``Artifact``. Emits ``McpToolCalled``.

        On a protocol/transport failure, returns ``Artifact(type="error")``
        after ``McpError`` has been emitted by the RPC layer.
        """
        session_id = self._by_server.get(server_url, f"adhoc:{server_url}")
        args_hash = hashlib.sha256(
            json.dumps(arguments, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        started = self._clock()
        try:
            if self._resilience is not None:
                # ADR-031: guard the call with a per-server circuit breaker and
                # (optionally) retry transient failures. The circuit sees the
                # McpGatewayError raised by _rpc so it can count failures / trip.
                async def _do_rpc():
                    return await self._rpc(
                        server_url, "tools/call", {"name": tool_name, "arguments": arguments}
                    )

                async def _guarded():
                    async with self._resilience.call_with_circuit(f"mcp:{server_url}"):
                        return await _do_rpc()

                result = await self._resilience.retry(_guarded, task_id=f"mcp:{server_url}:{tool_name}")
            else:
                result = await self._rpc(
                    server_url, "tools/call", {"name": tool_name, "arguments": arguments}
                )
        except (McpGatewayError, CircuitBreakerOpenError, RetryExhaustedError) as exc:
            latency_ms = (self._clock() - started) * 1000.0
            if self._store is not None:
                self._store.put_call(uuid.uuid4().hex, session_id, tool_name, args_hash, latency_ms, error=str(exc))
            if self._metrics is not None:
                await self._metrics.record_metric(
                    "mcp.tool_errors", 1.0, labels={"server_url": server_url, "tool": tool_name}
                )
            return Artifact(
                type="error",
                content={"error": str(exc), "tool": tool_name, "server_url": server_url},
                format="json",
                source=f"mcp:{server_url}",
            )
        latency_ms = (self._clock() - started) * 1000.0
        self._touch(session_id)
        await self._emit(McpToolCalled(session_id, tool_name, args_hash, latency_ms))
        if self._store is not None:
            self._store.put_call(uuid.uuid4().hex, session_id, tool_name, args_hash, latency_ms)
        if self._metrics is not None:
            await self._metrics.record_metric(
                "mcp.tool_latency_ms",
                latency_ms,
                labels={
                    "server_url": server_url,
                    "tool": tool_name,
                    "mcp_auth_source": self.auth_source(server_url),  # ADR-030 audit context
                },
            )
        return Artifact(
            type="mcp_tool_result",
            content=result,
            format="json",
            source=f"mcp:{server_url}::{tool_name}",
        )

    # -- resources ----------------------------------------------------------------- #
    async def read_resource(self, server_url: str, uri: str) -> str:
        """JSON-RPC ``resources/read`` → text. Emits ``McpResourceRead``."""
        result = await self._rpc(server_url, "resources/read", {"uri": uri})
        contents = result.get("contents", [])
        text = "".join(c.get("text", "") for c in contents)
        session_id = self._by_server.get(server_url, f"adhoc:{server_url}")
        self._touch(session_id)
        await self._emit(McpResourceRead(session_id, uri, len(text.encode("utf-8"))))
        return text

    # -- discovery / capability mapping ----------------------------------------------- #
    def discover_local_tools(self) -> list[McpTool]:
        """All cached tools (memory cache first, store fallback)."""
        cached = [t for tools in self._tools.values() for t in tools]
        if cached:
            return cached
        if self._store is not None:
            return self._store.list_tools()
        return []

    def resolve_capability(self, capability_name: str) -> McpTool | None:
        """Map a capability name to a cached MCP tool.

        Accepts ``mcp:<server_url>::<tool>``, ``mcp:<tool>`` or a bare tool
        name. Exact match first; then a ``prefix.*`` wildcard match.
        """
        name = capability_name
        if name.startswith("mcp:"):
            name = name[4:]
        if "::" in name:
            server_url, tool_name = name.split("::", 1)
            for tool in self._tools.get(server_url, []):
                if tool.name == tool_name:
                    return tool
            if self._store is not None:
                return self._store.get_tool(tool_name, server_url)
            return None
        tools = self.discover_local_tools()
        for tool in tools:
            if tool.name == name:
                return tool
        if name.endswith(".*") or name.endswith("*"):
            prefix = name.rstrip("*").rstrip(".")
            for tool in tools:
                if tool.name.startswith(prefix):
                    return tool
        return None

    def get_session(self, session_id: str) -> McpSession | None:
        return self._sessions.get(session_id)

    def auth_source(self, server_url: str) -> str:
        """ADR-030: audit context — how the auth token was obtained for a server.

        Returns ``"explicit"`` (passed to connect), ``"vault"`` (resolved from
        the ConfigVault), or ``"none"`` (no auth / never connected).
        """
        return self._auth_source.get(server_url, "none")

    # -- helpers ---------------------------------------------------------------------- #
    def _touch(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if session is not None:
            from datetime import datetime, timezone

            session.last_used = datetime.now(timezone.utc)

    async def _emit(self, event: Any) -> None:
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001 - persistence must never break the call
                pass
        if self._bus is not None:
            self._bus.publish(event)


__all__ = ["McpGateway", "McpGatewayError", "MCP_PROTOCOL_VERSION"]
