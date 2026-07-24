"""kernel/swarm_store.py — Swarm state persistence (ADR-023).

AXIS CONTRACT: depends only on ``kernel.domain`` + stdlib. Never imports
plugins/ or mcp/.

In-memory CRUD + optional SQLite persistence for :class:`kernel.domain.Swarm`
and :class:`kernel.domain.TaskDelegation`, mirroring the ``HumanProfileStore``
pattern from ADR-022.
"""

from __future__ import annotations

import sqlite3
from typing import Optional

from kernel.domain import Swarm, TaskDelegation


class SwarmStore:
    """In-memory CRUD for swarms + delegations, with optional SQLite backing.

    When ``db_path`` is ``None`` (default) state is purely in-memory. When a path
    is given, every mutation is persisted and the store reloads on construction
    (so a fresh instance sees previously-saved state).
    """

    def __init__(self, db_path: Optional[str] = None) -> None:
        self._db_path = db_path
        self._mem: dict[str, Swarm] = {}
        self._delegations: dict[str, TaskDelegation] = {}
        if db_path is not None:
            self._init_db()
            self._load_all()

    # -- persistence ------------------------------------------------------ #
    def _init_db(self) -> None:
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS swarms "
                "(swarm_id TEXT PRIMARY KEY, data TEXT)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS delegations "
                "(delegation_id TEXT PRIMARY KEY, swarm_id TEXT, data TEXT)"
            )
            conn.commit()
        finally:
            conn.close()

    def _load_all(self) -> None:
        assert self._db_path is not None
        conn = sqlite3.connect(self._db_path)
        try:
            for swarm_id, data in conn.execute("SELECT swarm_id, data FROM swarms"):
                self._mem[swarm_id] = Swarm.model_validate_json(data)
            for _, _, data in conn.execute("SELECT delegation_id, swarm_id, data FROM delegations"):
                d = TaskDelegation.model_validate_json(data)
                self._delegations[d.delegation_id] = d
        finally:
            conn.close()

    def _persist_swarm(self, swarm: Swarm) -> None:
        if self._db_path is None:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO swarms (swarm_id, data) VALUES (?, ?)",
                (swarm.swarm_id, swarm.model_dump_json()),
            )
            conn.commit()
        finally:
            conn.close()

    def _persist_delegation(self, d: TaskDelegation) -> None:
        if self._db_path is None:
            return
        conn = sqlite3.connect(self._db_path)
        try:
            conn.execute(
                "INSERT OR REPLACE INTO delegations (delegation_id, swarm_id, data) VALUES (?, ?, ?)",
                (d.delegation_id, d.swarm_id, d.model_dump_json()),
            )
            conn.commit()
        finally:
            conn.close()

    # -- swarm CRUD ------------------------------------------------------- #
    def put(self, swarm: Swarm) -> None:
        self._mem[swarm.swarm_id] = swarm
        self._persist_swarm(swarm)

    def get(self, swarm_id: str) -> Optional[Swarm]:
        return self._mem.get(swarm_id)

    def list(self) -> list[Swarm]:
        return list(self._mem.values())

    def delete(self, swarm_id: str) -> bool:
        if swarm_id not in self._mem:
            return False
        del self._mem[swarm_id]
        if self._db_path is not None:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.execute("DELETE FROM swarms WHERE swarm_id = ?", (swarm_id,))
                conn.execute("DELETE FROM delegations WHERE swarm_id = ?", (swarm_id,))
                conn.commit()
            finally:
                conn.close()
        return True

    # -- delegation history --------------------------------------------- #
    def put_delegation(self, d: TaskDelegation) -> None:
        self._delegations[d.delegation_id] = d
        self._persist_delegation(d)

    def get_delegation(self, delegation_id: str) -> Optional[TaskDelegation]:
        return self._delegations.get(delegation_id)

    def delegations_for(self, swarm_id: str) -> list[TaskDelegation]:
        return [d for d in self._delegations.values() if d.swarm_id == swarm_id]


__all__ = ["SwarmStore"]
