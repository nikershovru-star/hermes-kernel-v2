"""kernel/mcp_domain.py — MCP Gateway / Protocol Adapter domain (ADR-029).

Isolated ADR-local model set (established pattern: ``semantic_graph.py`` /
``marketplace_domain.py`` / ``observability_domain.py`` / ``security_domain.py``).
Axis-clean: stdlib ``datetime`` + pydantic only.

Naming note: ``kernel.domain.McpSessionEvent`` (ADR-008, SSE replay backlog)
and ``mcp/server.py:MCPServer`` (transport-layer server, different casing /
different package) are UNRELATED to these client-side gateway models — the
collision scan confirmed no shadowing.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class McpServer(BaseModel):
    """A remote MCP server the gateway can talk to (client-side view)."""

    url: str
    name: str = ""
    version: str = ""
    auth_token: str | None = None
    capabilities: list[str] = Field(default_factory=list)


class McpTool(BaseModel):
    """A tool exposed by a remote MCP server (from ``tools/list``)."""

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    server_url: str = ""


class McpResource(BaseModel):
    """A resource exposed by a remote MCP server (from ``resources/*``)."""

    uri: str
    mime_type: str = "text/plain"
    server_url: str = ""


class McpSession(BaseModel):
    """A logical client session with a remote MCP server.

    Status lifecycle: ``active`` → ``closed`` | ``error``.
    """

    session_id: str
    server_url: str
    status: str = "active"  # "active" | "closed" | "error"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_used: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
