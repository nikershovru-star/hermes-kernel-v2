"""kernel/security_store.py — security persistence (ADR-028).

In-memory CRUD + optional SQLite, mirroring ``PlanStore`` / ``GraphStore`` /
``MarketplaceStore`` / ``ObservabilityStore``. Tables: ``policies``, ``grants``,
``audit``. On ``db_path`` the store reloads rows into memory on construction.

AXIS: imports only ``kernel.security_domain``. No ``kernel.events`` / ``pydantic``
registry coupling.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Any

from kernel.security_domain import AuditEntry, Permission, SandboxPolicy


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SecurityStore:
    """Persist sandbox policies, layered grants and audit entries."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        self._mem_policies: dict[str, SandboxPolicy] = {}
        self._mem_grants: dict[str, list[Permission]] = {}
        self._mem_audit: list[AuditEntry] = []
        if db_path:
            self._conn = sqlite3.connect(db_path, check_same_thread=False)
            self._init_db()
            self._load_all()

    # -- schema ---------------------------------------------------------- #
    def _init_db(self) -> None:
        assert self._conn is not None
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS policies "
            "(package_id TEXT PRIMARY KEY, data TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS grants "
            "(id TEXT PRIMARY KEY, package_id TEXT, data TEXT)"
        )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS audit "
            "(entry_id TEXT PRIMARY KEY, who TEXT, action TEXT, resource TEXT, "
            "result TEXT, ts TEXT, data TEXT)"
        )
        self._conn.commit()

    def _load_all(self) -> None:
        assert self._conn is not None
        for row in self._conn.execute("SELECT package_id, data FROM policies"):
            self._mem_policies[row[0]] = SandboxPolicy.model_validate_json(row[1])
        for row in self._conn.execute("SELECT package_id, data FROM grants"):
            self._mem_grants.setdefault(row[0], []).append(
                Permission.model_validate_json(row[1])
            )
        for row in self._conn.execute(
            "SELECT entry_id, who, action, resource, result, ts, data FROM audit"
        ):
            self._mem_audit.append(AuditEntry.model_validate_json(row[6]))

    # -- policies -------------------------------------------------------- #
    def put_policy(self, package_id: str, policy: SandboxPolicy) -> None:
        self._mem_policies[package_id] = policy
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO policies (package_id, data) VALUES (?, ?)",
                (package_id, policy.model_dump_json()),
            )
            self._conn.commit()

    def get_policy(self, package_id: str) -> SandboxPolicy | None:
        return self._mem_policies.get(package_id)

    # -- grants ---------------------------------------------------------- #
    def put_grant(self, package_id: str, permission: Permission) -> None:
        self._mem_grants.setdefault(package_id, []).append(permission)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO grants (id, package_id, data) VALUES (?, ?, ?)",
                (permission.action + ":" + permission.resource + ":" + package_id,
                 package_id, permission.model_dump_json()),
            )
            self._conn.commit()

    def get_grant(self, package_id: str) -> list[Permission]:
        return list(self._mem_grants.get(package_id, []))

    # -- audit ----------------------------------------------------------- #
    def put_audit(self, entry: AuditEntry) -> None:
        self._mem_audit.append(entry)
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit "
                "(entry_id, who, action, resource, result, ts, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.who,
                    entry.action,
                    entry.resource,
                    entry.result,
                    entry.timestamp.isoformat(),
                    entry.model_dump_json(),
                ),
            )
            self._conn.commit()

    def list_audit(
        self,
        principal: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AuditEntry]:
        out = self._mem_audit
        if principal is not None:
            out = [e for e in out if e.who == principal]
        if since is not None:
            out = [e for e in out if e.timestamp >= since]
        if until is not None:
            out = [e for e in out if e.timestamp <= until]
        return out

    def close(self) -> None:
        """Close the underlying SQLite connection (if any)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None
