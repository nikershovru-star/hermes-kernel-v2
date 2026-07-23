"""kernel/persistence.py — SQLite persistence for domain entities (P5.3).

A single universal table stores every entity as ``(id, type, workspace_id,
data_json)``. Pydantic entities are serialised via ``model_dump_json`` and
rehydrated by type. This avoids fragile per-field DDL generation while keeping
the store schema-stable and workspace-isolated.

AXIS CONTRACT: depends on kernel.domain only. The public API is async, but the
DB work runs on a **single dedicated worker thread** (one-thread
``ThreadPoolExecutor``) so a single sqlite3 connection is reused safely and
``:memory:`` databases are shared across calls. No external async driver needed.

Storage shape
-------------
    CREATE TABLE entities (
        id           TEXT PRIMARY KEY,
        type         TEXT NOT NULL,
        workspace_id TEXT NOT NULL,
        data         TEXT NOT NULL          -- model_dump_json(entity)
    )
    CREATE TABLE markers (
        key          TEXT PRIMARY KEY,
        workspace_id TEXT NOT NULL,
        value        TEXT
    )

Every entity query is filtered by ``workspace_id`` (ADR-007 isolation).
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional, Type, TypeVar

from kernel.domain import (
    BaseEntity,
    Chunk,
    Document,
    KnowledgeNode,
    McpSessionEvent,
    Relation,
    Workspace,
    ENTITY_TYPES,
)

T = TypeVar("T", bound=BaseEntity)

# entity type -> concrete Pydantic class (for rehydration)
_TYPE_TO_CLASS: dict[str, Type[BaseEntity]] = {
    "Document": Document,
    "Chunk": Chunk,
    "KnowledgeNode": KnowledgeNode,
    "Relation": Relation,
    "Workspace": Workspace,
    "McpSessionEvent": McpSessionEvent,
}


class PersistenceRegistry:
    """Async CRUD over a SQLite store, workspace-isolated.

    All DB calls execute on a single dedicated thread (one connection), so the
    async API is safe and in-memory DBs persist across calls.
    """

    def __init__(self, db_path: str = ":memory:") -> None:
        self._db_path = db_path
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="hermes-persist")
        self._conn: Optional[sqlite3.Connection] = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS entities ("
                " id TEXT PRIMARY KEY, type TEXT NOT NULL,"
                " workspace_id TEXT NOT NULL, data TEXT NOT NULL)"
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS markers ("
                " key TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, value TEXT)"
            )
        return self._conn

    def _run(self, fn):
        """Run a sync DB callable on the dedicated thread; return its result."""
        loop = asyncio.get_event_loop()
        return loop.run_in_executor(self._executor, fn)

    async def close(self) -> None:
        def _do() -> None:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

        await self._run(_do)
        self._executor.shutdown(wait=True)

    # -- CRUD ------------------------------------------------------------- #
    async def save(self, entity: BaseEntity) -> str:
        """Upsert an entity (idempotent: repeat save = UPDATE, no duplicate)."""

        def _do() -> str:
            conn = self._get_conn()
            conn.execute(
                "INSERT INTO entities (id, type, workspace_id, data)"
                " VALUES (?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET"
                " type=excluded.type, workspace_id=excluded.workspace_id,"
                " data=excluded.data",
                (
                    entity.id,
                    entity.__class__.__name__,
                    entity.workspace_id,
                    entity.model_dump_json(),
                ),
            )
            conn.commit()
            return entity.id

        return await self._run(_do)

    async def get(self, id: str) -> Optional[BaseEntity]:
        def _do() -> Optional[dict]:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT type, data FROM entities WHERE id = ?", (id,)
            ).fetchone()
            return dict(row) if row else None

        row = await self._run(_do)
        if row is None:
            return None
        return self._rehydrate(row["type"], row["data"])

    async def list(
        self, workspace_id: str, entity_type: Optional[str] = None
    ) -> list[BaseEntity]:
        def _do() -> list[dict]:
            conn = self._get_conn()
            if entity_type:
                rows = conn.execute(
                    "SELECT type, data FROM entities"
                    " WHERE workspace_id = ? AND type = ? ORDER BY id",
                    (workspace_id, entity_type),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT type, data FROM entities"
                    " WHERE workspace_id = ? ORDER BY id",
                    (workspace_id,),
                ).fetchall()
            return [dict(r) for r in rows]

        rows = await self._run(_do)
        return [self._rehydrate(r["type"], r["data"]) for r in rows]

    async def list_all(self, entity_type: Optional[str] = None) -> list[BaseEntity]:
        """List every entity across all workspaces (admin/restore use)."""

        def _do() -> list[dict]:
            conn = self._get_conn()
            if entity_type:
                rows = conn.execute(
                    "SELECT type, data FROM entities WHERE type = ? ORDER BY id",
                    (entity_type,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT type, data FROM entities ORDER BY id"
                ).fetchall()
            return [dict(r) for r in rows]

        rows = await self._run(_do)
        return [self._rehydrate(r["type"], r["data"]) for r in rows]

    async def delete(self, id: str) -> bool:
        def _do() -> bool:
            conn = self._get_conn()
            cur = conn.execute("DELETE FROM entities WHERE id = ?", (id,))
            conn.commit()
            return cur.rowcount > 0

        return await self._run(_do)

    async def exists(self, id: str) -> bool:
        def _do() -> bool:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT 1 FROM entities WHERE id = ?", (id,)
            ).fetchone()
            return row is not None

        return await self._run(_do)

    async def count(self, workspace_id: str) -> int:
        def _do() -> int:
            conn = self._get_conn()
            row = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE workspace_id = ?",
                (workspace_id,),
            ).fetchone()
            return int(row[0])

        return await self._run(_do)

    async def mark(self, key: str, workspace_id: str, value: str = "1") -> None:
        def _do() -> None:
            conn = self._get_conn()
            conn.execute(
                "INSERT OR REPLACE INTO markers (key, workspace_id, value)"
                " VALUES (?, ?, ?)",
                (key, workspace_id, value),
            )
            conn.commit()

        await self._run(_do)

    def _contains_marker(self, key: str) -> bool:
        """Synchronous marker check for FileScanner's sync sweep.

        Uses its own ``check_same_thread=False`` connection so it is callable
        from the scanner thread. For ``:memory:`` DBs the marker table is not
        shared with the executor's connection, so callers should use a
        file-backed DB when relying on scanner de-duplication.
        """
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS markers ("
                " key TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, value TEXT)"
            )
            row = conn.execute(
                "SELECT 1 FROM markers WHERE key = ?", (key,)
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    # -- helpers ---------------------------------------------------------- #
    @staticmethod
    def _rehydrate(type_name: str, data: str) -> BaseEntity:
        cls = _TYPE_TO_CLASS.get(type_name, BaseEntity)
        return cls.model_validate_json(data)
