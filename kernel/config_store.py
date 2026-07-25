"""kernel/config_store.py — Configuration & Secrets persistence (ADR-030).

SQLite-backed with a pure in-memory fallback when ``db_path=None`` (mirrors
``McpStore`` / ``MarketplaceStore`` / ``ObservabilityStore``). Three tables:

  * ``config``       — plaintext + encrypted-marker config entries (soft delete).
  * ``secrets``      — encrypted secret payloads (ciphertext/nonce/tag).
  * ``config_audit`` — access audit trail (who/action/resource/result/when).

The nullable connection handle is initialized to ``None`` BEFORE the ``if
db_path`` block (ADR-026 lesson: never leave ``self._conn`` undefined for the
in-memory path). ``reload(db_path)`` re-points the store at a new database file.

AXIS: imports only ``kernel.config_domain`` + ``kernel.security_domain`` (+
stdlib). Self-contained leaf.
"""

from __future__ import annotations

import base64
import sqlite3
from datetime import datetime, timezone

from kernel.config_domain import ConfigEntry, ConfigScope, SecretValue
from kernel.security_domain import AuditEntry


def _b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii")) if text else b""


def _parse_dt(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


class ConfigStore:
    """Persistence for config entries, secrets and the access audit log."""

    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path
        self._conn: sqlite3.Connection | None = None
        # in-memory fallback maps (sole backing when db_path is None)
        self._config: dict[str, ConfigEntry] = {}
        self._secrets: dict[str, SecretValue] = {}
        self._audit: list[AuditEntry] = []
        if db_path is not None:
            self._conn = sqlite3.connect(db_path)
            self._conn.row_factory = sqlite3.Row
            self._ensure_schema()

    # -- schema / lifecycle --------------------------------------------- #
    def _ensure_schema(self) -> None:
        assert self._conn is not None
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS config (
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                encrypted INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (scope, scope_id, key)
            );
            CREATE TABLE IF NOT EXISTS secrets (
                scope TEXT NOT NULL,
                scope_id TEXT NOT NULL,
                key TEXT NOT NULL,
                ciphertext TEXT NOT NULL,
                nonce TEXT NOT NULL DEFAULT '',
                tag TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                rotated_at TEXT NOT NULL,
                PRIMARY KEY (scope, scope_id, key)
            );
            CREATE TABLE IF NOT EXISTS config_audit (
                entry_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                accessor TEXT NOT NULL,
                action TEXT NOT NULL,
                resource TEXT NOT NULL,
                result TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT ''
            );
            """
        )
        self._conn.commit()

    def reload(self, db_path: str | None = None) -> None:
        """Re-open the store against ``db_path`` (or the current path)."""
        path = db_path or self._db_path
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        self._db_path = path
        if path is not None:
            self._conn = sqlite3.connect(path)
            self._conn.row_factory = sqlite3.Row
            self._ensure_schema()

    @staticmethod
    def _sid(scope_id: str | None) -> str:
        return scope_id or "__global__"

    @staticmethod
    def _unsid(scope_id: str) -> str | None:
        return None if scope_id == "__global__" else scope_id

    @classmethod
    def _key(cls, key: str, scope: ConfigScope, scope_id: str | None) -> str:
        return f"{scope.value}:{cls._sid(scope_id)}:{key}"

    # -- config --------------------------------------------------------- #
    def put_config(self, entry: ConfigEntry) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO config "
                "(scope, scope_id, key, value, encrypted, version, updated_at, deleted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.scope.value,
                    self._sid(entry.scope_id),
                    entry.key,
                    entry.value,
                    1 if entry.encrypted else 0,
                    entry.version,
                    entry.updated_at.isoformat(),
                    1 if entry.deleted else 0,
                ),
            )
            self._conn.commit()
        else:
            self._config[self._key(entry.key, entry.scope, entry.scope_id)] = entry

    def get_config(
        self, key: str, scope: ConfigScope, scope_id: str | None
    ) -> ConfigEntry | None:
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT * FROM config WHERE scope = ? AND scope_id = ? AND key = ?",
                (scope.value, self._sid(scope_id), key),
            ).fetchone()
            return self._row_to_config(row) if row else None
        return self._config.get(self._key(key, scope, scope_id))

    def delete_config(
        self, key: str, scope: ConfigScope, scope_id: str | None
    ) -> bool:
        """Soft-delete: mark ``deleted=1`` and bump version. Returns success."""
        entry = self.get_config(key, scope, scope_id)
        if entry is None:
            return False
        entry.deleted = True
        entry.version += 1
        entry.updated_at = datetime.now(timezone.utc)
        self.put_config(entry)
        return True

    def list_config(
        self,
        scope: ConfigScope | None = None,
        scope_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[ConfigEntry]:
        entries: list[ConfigEntry] = []
        if self._conn is not None:
            rows = self._conn.execute("SELECT * FROM config").fetchall()
            entries = [self._row_to_config(r) for r in rows]
        else:
            entries = list(self._config.values())
        out: list[ConfigEntry] = []
        for e in entries:
            if scope is not None and e.scope != scope:
                continue
            if scope is not None and (scope_id or None) != (e.scope_id or None):
                continue
            if e.deleted and not include_deleted:
                continue
            out.append(e)
        return out

    def _row_to_config(self, row: sqlite3.Row) -> ConfigEntry:
        return ConfigEntry(
            key=row["key"],
            value=row["value"],
            scope=ConfigScope(row["scope"]),
            scope_id=self._unsid(row["scope_id"]),
            version=row["version"],
            updated_at=_parse_dt(row["updated_at"]),
            encrypted=bool(row["encrypted"]),
            deleted=bool(row["deleted"]),
        )

    # -- secrets -------------------------------------------------------- #
    def put_secret(self, secret: SecretValue) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO secrets "
                "(scope, scope_id, key, ciphertext, nonce, tag, version, rotated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    secret.scope.value,
                    self._sid(secret.scope_id),
                    secret.key,
                    _b64e(secret.ciphertext),
                    _b64e(secret.nonce),
                    _b64e(secret.tag),
                    secret.version,
                    secret.rotated_at.isoformat(),
                ),
            )
            self._conn.commit()
        else:
            self._secrets[self._key(secret.key, secret.scope, secret.scope_id)] = secret

    def get_secret(
        self, key: str, scope: ConfigScope, scope_id: str | None
    ) -> SecretValue | None:
        if self._conn is not None:
            row = self._conn.execute(
                "SELECT * FROM secrets WHERE scope = ? AND scope_id = ? AND key = ?",
                (scope.value, self._sid(scope_id), key),
            ).fetchone()
            return self._row_to_secret(row) if row else None
        return self._secrets.get(self._key(key, scope, scope_id))

    def rotate_secret(self, secret: SecretValue) -> None:
        """Overwrite the stored secret with the rotated payload.

        NOTE (honest limitation): rotation OVERWRITES — the previous ciphertext
        is NOT preserved (no version history table). ``test_config_store``
        asserts this overwrite semantics explicitly.
        """
        self.put_secret(secret)

    def list_secrets(
        self, scope: ConfigScope | None = None, scope_id: str | None = None
    ) -> list[SecretValue]:
        secrets: list[SecretValue] = []
        if self._conn is not None:
            rows = self._conn.execute("SELECT * FROM secrets").fetchall()
            secrets = [self._row_to_secret(r) for r in rows]
        else:
            secrets = list(self._secrets.values())
        out: list[SecretValue] = []
        for s in secrets:
            if scope is not None and s.scope != scope:
                continue
            if scope is not None and (scope_id or None) != (s.scope_id or None):
                continue
            out.append(s)
        return out

    def _row_to_secret(self, row: sqlite3.Row) -> SecretValue:
        return SecretValue(
            key=row["key"],
            scope=ConfigScope(row["scope"]),
            scope_id=self._unsid(row["scope_id"]),
            ciphertext=_b64d(row["ciphertext"]),
            nonce=_b64d(row["nonce"]),
            tag=_b64d(row["tag"]),
            version=row["version"],
            rotated_at=_parse_dt(row["rotated_at"]),
        )

    # -- audit ---------------------------------------------------------- #
    def put_audit(self, entry: AuditEntry) -> None:
        if self._conn is not None:
            self._conn.execute(
                "INSERT OR REPLACE INTO config_audit "
                "(entry_id, timestamp, accessor, action, resource, result, detail) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    entry.entry_id,
                    entry.timestamp.isoformat(),
                    entry.who,
                    entry.action,
                    entry.resource,
                    entry.result,
                    entry.detail,
                ),
            )
            self._conn.commit()
        else:
            self._audit.append(entry)

    def list_audit(
        self,
        key: str | None = None,
        accessor: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AuditEntry]:
        entries: list[AuditEntry] = []
        if self._conn is not None:
            rows = self._conn.execute(
                "SELECT * FROM config_audit ORDER BY timestamp"
            ).fetchall()
            entries = [self._row_to_audit(r) for r in rows]
        else:
            entries = list(self._audit)
        out: list[AuditEntry] = []
        for e in entries:
            if key is not None and e.resource != f"secret:{key}":
                continue
            if accessor is not None and e.who != accessor:
                continue
            if since is not None and e.timestamp < since:
                continue
            if until is not None and e.timestamp > until:
                continue
            out.append(e)
        return out

    def _row_to_audit(self, row: sqlite3.Row) -> AuditEntry:
        return AuditEntry(
            entry_id=row["entry_id"],
            who=row["accessor"],
            action=row["action"],
            resource=row["resource"],
            result=row["result"],
            timestamp=_parse_dt(row["timestamp"]),
            detail=row["detail"],
        )


__all__ = ["ConfigStore"]
