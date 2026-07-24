"""kernel/mcp_store.py — persistence for the MCP Gateway (ADR-029).

SQLite-backed with a pure in-memory fallback when ``db_path=None`` (mirrors
``PlanStore`` / ``GraphStore`` / ``MarketplaceStore``). Axis: imports only
``kernel.mcp_domain`` + stdlib.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from kernel.mcp_domain import McpServer, McpSession, McpTool


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class McpStore:
    """Persistence for MCP servers, tools, sessions and call records."""

    def __init__(self, db_path: str | None = None) -> None:
        # PITFALL (ADR-026): initialize the nullable connection BEFORE the
        # conditional so the in-memory path never hits AttributeError.
        self._conn: sqlite3.Connection | None = None
        self._servers: dict[str, McpServer] = {}
        self._tools: dict[tuple[str, str], McpTool] = {}
        self._sessions: dict[str, McpSession] = {}
        self._calls: list[dict[str, Any]] = []
        if db_path is not None:
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            self._create_tables()

    # -- schema ----------------------------------------------------------- #
    def _create_tables(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS servers (
                url TEXT PRIMARY KEY,
                name TEXT NOT NULL DEFAULT '',
                version TEXT NOT NULL DEFAULT '',
                auth_token TEXT,
                capabilities_json TEXT NOT NULL DEFAULT '[]'
            );
            CREATE TABLE IF NOT EXISTS tools (
                name TEXT NOT NULL,
                server_url TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                schema_json TEXT NOT NULL DEFAULT '{}',
                PRIMARY KEY (name, server_url)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                server_url TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_used TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                arguments_hash TEXT NOT NULL DEFAULT '',
                latency_ms REAL NOT NULL DEFAULT 0.0,
                error TEXT,
                timestamp TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # -- servers ----------------------------------------------------------- #
    def put_server(self, server: McpServer) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO servers (url, name, version, auth_token, capabilities_json) "
                "VALUES (?, ?, ?, ?, ?)",
                (server.url, server.name, server.version, server.auth_token, json.dumps(server.capabilities)),
            )
            self._conn.commit()
            return
        self._servers[server.url] = server

    def get_server(self, url: str) -> McpServer | None:
        if self._conn is not None:
            row = self._conn.execute("SELECT * FROM servers WHERE url = ?", (url,)).fetchone()
            if row is None:
                return None
            return McpServer(
                url=row["url"],
                name=row["name"],
                version=row["version"],
                auth_token=row["auth_token"],
                capabilities=json.loads(row["capabilities_json"]),
            )
        return self._servers.get(url)

    # -- tools -------------------------------------------------------------- #
    def put_tool(self, tool: McpTool) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO tools (name, server_url, description, schema_json) VALUES (?, ?, ?, ?)",
                (tool.name, tool.server_url, tool.description, json.dumps(tool.input_schema)),
            )
            self._conn.commit()
            return
        self._tools[(tool.server_url, tool.name)] = tool

    def get_tool(self, name: str, server_url: str) -> McpTool | None:
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT * FROM tools WHERE name = ? AND server_url = ?", (name, server_url)
            ).fetchone()
            if row is None:
                return None
            return self._row_to_tool(row)
        return self._tools.get((server_url, name))

    def list_tools(self, server_url: str | None = None) -> list[McpTool]:
        if self._conn is not None:
            if server_url is not None:
                rows = self._conn.execute("SELECT * FROM tools WHERE server_url = ?", (server_url,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM tools").fetchall()
            return [self._row_to_tool(r) for r in rows]
        tools = list(self._tools.values())
        if server_url is not None:
            tools = [t for t in tools if t.server_url == server_url]
        return tools

    @staticmethod
    def _row_to_tool(row: sqlite3.Row) -> McpTool:
        return McpTool(
            name=row["name"],
            server_url=row["server_url"],
            description=row["description"],
            input_schema=json.loads(row["schema_json"]),
        )

    # -- sessions ------------------------------------------------------------ #
    def put_session(self, session: McpSession) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO sessions (session_id, server_url, status, created_at, last_used) "
                "VALUES (?, ?, ?, ?, ?)",
                (session.session_id, session.server_url, session.status, _iso(session.created_at), _iso(session.last_used)),
            )
            self._conn.commit()
            return
        self._sessions[session.session_id] = session

    def get_session(self, session_id: str) -> McpSession | None:
        if self._conn is not None:
            row = self._conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
            if row is None:
                return None
            return McpSession(
                session_id=row["session_id"],
                server_url=row["server_url"],
                status=row["status"],
                created_at=_parse(row["created_at"]),
                last_used=_parse(row["last_used"]),
            )
        return self._sessions.get(session_id)

    # -- calls ---------------------------------------------------------------- #
    def put_call(
        self,
        call_id: str,
        session_id: str,
        tool_name: str,
        arguments_hash: str = "",
        latency_ms: float = 0.0,
        error: str | None = None,
        timestamp: datetime | None = None,
    ) -> None:
        ts = timestamp or datetime.now(timezone.utc)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO calls (call_id, session_id, tool_name, arguments_hash, latency_ms, error, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (call_id, session_id, tool_name, arguments_hash, latency_ms, error, _iso(ts)),
            )
            self._conn.commit()
            return
        self._calls.append(
            {
                "call_id": call_id,
                "session_id": session_id,
                "tool_name": tool_name,
                "arguments_hash": arguments_hash,
                "latency_ms": latency_ms,
                "error": error,
                "timestamp": ts,
            }
        )

    def list_calls(
        self,
        session_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[dict[str, Any]]:
        if self._conn is not None:
            rows = self._conn.execute("SELECT * FROM calls ORDER BY timestamp").fetchall()
            calls = [
                {
                    "call_id": r["call_id"],
                    "session_id": r["session_id"],
                    "tool_name": r["tool_name"],
                    "arguments_hash": r["arguments_hash"],
                    "latency_ms": r["latency_ms"],
                    "error": r["error"],
                    "timestamp": _parse(r["timestamp"]),
                }
                for r in rows
            ]
        else:
            calls = list(self._calls)
        if session_id is not None:
            calls = [c for c in calls if c["session_id"] == session_id]
        if since is not None:
            calls = [c for c in calls if c["timestamp"] >= since]
        if until is not None:
            calls = [c for c in calls if c["timestamp"] <= until]
        return calls

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None


__all__ = ["McpStore"]
