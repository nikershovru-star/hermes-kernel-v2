"""kernel/resilience_store.py — Resilience Platform persistence (ADR-031).

SQLite-backed with a pure in-memory fallback when ``db_path=None`` (mirrors
``ConfigStore`` / ``McpStore`` / ``MarketplaceStore``). Three tables:

  * ``circuits``    — per-circuit state snapshot (state/failure_count/config).
  * ``retries``     — retry attempt journal (task_id/attempt/backoff/error).
  * ``dead_letter`` — parked failed tasks with replay lifecycle status.

The nullable connection handle is initialized to ``None`` BEFORE the ``if
db_path`` block (ADR-026 lesson). ``reload(db_path)`` re-points the store at a
new database file (repo-reload).

AXIS: imports only ``kernel.resilience_domain`` (+ stdlib). Self-contained leaf.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from kernel.resilience_domain import ResilienceDeadLetterEntry


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


class ResilienceStore:
    """Persistence for circuit state, retry journal and the dead-letter queue."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # in-memory fallback (sole backing when db_path is None)
        self._circuits: dict[str, dict] = {}
        self._retries: list[dict] = []
        self._dead_letter: dict[str, ResilienceDeadLetterEntry] = {}
        if db_path is not None:
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            self._ensure_schema()

    # -- schema / lifecycle --------------------------------------------- #
    def _ensure_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS circuits (
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                failure_count INTEGER NOT NULL DEFAULT 0,
                last_failure TEXT,
                config_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS retries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                attempt INTEGER NOT NULL,
                backoff_ms INTEGER NOT NULL,
                error TEXT NOT NULL,
                timestamp TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dead_letter (
                entry_id TEXT PRIMARY KEY,
                original_task_json TEXT NOT NULL,
                error TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                enqueued_at TEXT NOT NULL,
                last_attempt TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        self._conn.commit()

    def reload(self, db_path: str | None = "__keep__") -> None:
        """Re-open the store. Pass a new ``db_path`` to re-point; ``None`` drops
        to in-memory. Omit the arg to reconnect the same file (repo-reload)."""
        if db_path != "__keep__":
            self._db_path = db_path
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._db_path is not None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._ensure_schema()

    # -- circuits -------------------------------------------------------- #
    def put_circuit(self, name: str, state: str, failure_count: int, last_failure: datetime | None, config_json: str) -> None:
        lf = last_failure.isoformat() if last_failure else None
        if self._conn is not None:
            self._conn.execute(
                "INSERT INTO circuits (name, state, failure_count, last_failure, config_json) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "state=excluded.state, failure_count=excluded.failure_count, "
                "last_failure=excluded.last_failure, config_json=excluded.config_json",
                (name, state, failure_count, lf, config_json),
            )
            self._conn.commit()
        else:
            self._circuits[name] = {
                "name": name, "state": state, "failure_count": failure_count,
                "last_failure": lf, "config_json": config_json,
            }

    def get_circuit(self, name: str) -> dict | None:
        if self._conn is not None:
            row = self._conn.execute("SELECT * FROM circuits WHERE name = ?", (name,)).fetchone()
            return dict(row) if row else None
        return self._circuits.get(name)

    # -- retries --------------------------------------------------------- #
    def put_retry(self, task_id: str, attempt: int, backoff_ms: int, error: str, timestamp: datetime) -> None:
        ts = timestamp.isoformat()
        if self._conn is not None:
            self._conn.execute(
                "INSERT INTO retries (task_id, attempt, backoff_ms, error, timestamp) VALUES (?, ?, ?, ?, ?)",
                (task_id, attempt, backoff_ms, error, ts),
            )
            self._conn.commit()
        else:
            self._retries.append({
                "task_id": task_id, "attempt": attempt, "backoff_ms": backoff_ms,
                "error": error, "timestamp": ts,
            })

    def list_retries(self, task_id: str | None = None) -> list[dict]:
        if self._conn is not None:
            if task_id is not None:
                rows = self._conn.execute("SELECT * FROM retries WHERE task_id = ? ORDER BY id", (task_id,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM retries ORDER BY id").fetchall()
            return [dict(r) for r in rows]
        if task_id is not None:
            return [r for r in self._retries if r["task_id"] == task_id]
        return list(self._retries)

    # -- dead-letter ----------------------------------------------------- #
    def put_dead_letter(self, entry: ResilienceDeadLetterEntry) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT INTO dead_letter (entry_id, original_task_json, error, attempts, enqueued_at, last_attempt, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(entry_id) DO UPDATE SET "
                "error=excluded.error, attempts=excluded.attempts, "
                "last_attempt=excluded.last_attempt, status=excluded.status",
                (
                    entry.entry_id, json.dumps(entry.original_task), entry.error, entry.attempts,
                    entry.enqueued_at.isoformat(),
                    entry.last_attempt.isoformat() if entry.last_attempt else None,
                    entry.status,
                ),
            )
            self._conn.commit()
        else:
            self._dead_letter[entry.entry_id] = entry.model_copy(deep=True)

    def get_dead_letter(self, entry_id: str) -> ResilienceDeadLetterEntry | None:
        if self._conn is not None:
            row = self._conn.execute("SELECT * FROM dead_letter WHERE entry_id = ?", (entry_id,)).fetchone()
            return self._row_to_dle(row) if row else None
        entry = self._dead_letter.get(entry_id)
        return entry.model_copy(deep=True) if entry else None

    def list_dead_letter(self, status: str | None = "pending") -> list[ResilienceDeadLetterEntry]:
        if self._conn is not None:
            if status is not None:
                rows = self._conn.execute("SELECT * FROM dead_letter WHERE status = ? ORDER BY enqueued_at", (status,)).fetchall()
            else:
                rows = self._conn.execute("SELECT * FROM dead_letter ORDER BY enqueued_at").fetchall()
            return [self._row_to_dle(r) for r in rows]
        entries = [e.model_copy(deep=True) for e in self._dead_letter.values()]
        if status is not None:
            entries = [e for e in entries if e.status == status]
        return entries

    def update_dead_letter_status(self, entry_id: str, status: str, last_attempt: datetime | None = None) -> bool:
        la = last_attempt.isoformat() if last_attempt else None
        if self._conn is not None:
            cur = self._conn.execute(
                "UPDATE dead_letter SET status = ?, last_attempt = COALESCE(?, last_attempt) WHERE entry_id = ?",
                (status, la, entry_id),
            )
            self._conn.commit()
            return cur.rowcount > 0
        entry = self._dead_letter.get(entry_id)
        if entry is None:
            return False
        entry.status = status
        if last_attempt is not None:
            entry.last_attempt = last_attempt
        return True

    @staticmethod
    def _row_to_dle(row: sqlite3.Row) -> ResilienceDeadLetterEntry:
        return ResilienceDeadLetterEntry(
            entry_id=row["entry_id"],
            original_task=json.loads(row["original_task_json"]),
            error=row["error"],
            attempts=row["attempts"],
            enqueued_at=_parse_dt(row["enqueued_at"]) or datetime.now(timezone.utc),
            last_attempt=_parse_dt(row["last_attempt"]),
            status=row["status"],
        )
