"""kernel/config_vault.py — Configuration & Secrets engine (ADR-030).

``ConfigVault`` is the central, scope-aware store facade for configuration and
secrets used by agents, workflows, MCP servers and plugins. It is async-first
and fully injectable (store / event_bus / event_store / clock / cipher / sleep)
so it is deterministic under test and wireable in production.

Encryption model (honest): secrets are encrypted at rest via an INJECTABLE
async ``cipher`` (``encrypt(plaintext:str)->bytes`` / ``decrypt(ciphertext:
bytes)->str``). The default is a base64 stub (``_Base64Cipher``) — deterministic,
NOT secure; production wires Fernet (``cryptography``) or AES-256-GCM. There is
no HSM/KMS integration and no runtime memory zeroing (plaintext lives in RAM
during ``resolve_secret``).

AXIS: imports only ``kernel.config_domain`` + ``kernel.events`` +
``kernel.security_domain`` (shared ``AuditEntry``). Never imports plugins/mcp or
higher-level kernel engines.
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.config_domain import (
    ConfigEntry,
    ConfigScope,
    SecretRef,
    SecretValue,
)
from kernel.events import (
    ConfigAccessDenied,
    ConfigChanged,
    ConfigReloaded,
    EventBus,
    EventStore,
    SecretAccessed,
    SecretRotated,
)
from kernel.security_domain import AuditEntry


async def _default_sleep(_seconds: float) -> None:  # pragma: no cover - trivial
    return None


class _Base64Cipher:
    """Deterministic non-secure stub cipher (default).

    Encrypt = base64 of the UTF-8 plaintext; decrypt reverses it. ``nonce`` /
    ``tag`` are empty. Suitable ONLY for tests / dev — swap for Fernet or
    AES-256-GCM in production via the ``cipher=`` injection point.
    """

    async def encrypt(self, plaintext: str) -> bytes:
        return base64.b64encode(plaintext.encode("utf-8"))

    async def decrypt(self, ciphertext: bytes) -> str:
        return base64.b64decode(ciphertext).decode("utf-8")


class ConfigVault:
    """Scope-aware configuration + secrets vault (ADR-030).

    Config values are stored in plaintext (``ConfigEntry``); secrets are stored
    encrypted (``SecretValue``) and only ever decrypted through
    ``resolve_secret`` (which writes an audit entry). ``get`` never decrypts.
    """

    def __init__(
        self,
        store: Any | None = None,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        cipher: Any | None = None,
        sleep: Callable[[float], Awaitable[None]] = _default_sleep,
    ) -> None:
        self._store = store
        self._bus = event_bus
        self._event_store = event_store
        self._clock = clock
        # cipher is optional: resolve_secret raises RuntimeError when unwired.
        # set() of a secret also requires a cipher.
        self._cipher = cipher
        self._sleep = sleep
        # in-memory cache (mirrors store; sole backing when store is None)
        self._config: dict[str, ConfigEntry] = {}
        self._secrets: dict[str, SecretValue] = {}
        self._audit: list[AuditEntry] = []

    # -- helpers --------------------------------------------------------- #
    @staticmethod
    def _ckey(key: str, scope: ConfigScope, scope_id: str | None) -> str:
        return f"{scope.value}:{scope_id or 'global'}:{key}"

    async def _emit(self, event: Any) -> None:
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001 - persistence must never break a call
                pass
        if self._bus is not None:
            self._bus.publish(event)

    # -- config CRUD ----------------------------------------------------- #
    async def set(
        self,
        key: str,
        value: str,
        scope: ConfigScope = ConfigScope.GLOBAL,
        scope_id: str | None = None,
        changed_by: str = "system",
    ) -> ConfigEntry:
        """Store / update a plaintext config value; bump version; emit event."""
        ck = self._ckey(key, scope, scope_id)
        existing = self._config.get(ck)
        if existing is None and self._store is not None:
            existing = self._store.get_config(key, scope, scope_id)
        version = (existing.version + 1) if existing is not None else 1
        entry = ConfigEntry(
            key=key,
            value=value,
            scope=scope,
            scope_id=scope_id,
            version=version,
            updated_at=self._clock(),
            encrypted=False,
            deleted=False,
        )
        self._config[ck] = entry
        if self._store is not None:
            self._store.put_config(entry)
        await self._emit(ConfigChanged(scope.value, scope_id, key, version, changed_by))
        return entry

    async def get(
        self,
        key: str,
        scope: ConfigScope = ConfigScope.GLOBAL,
        scope_id: str | None = None,
        default: str | None = None,
    ) -> str | None:
        """Read a config value (raw). Never decrypts; excludes soft-deleted.

        For an ``encrypted`` entry this returns the raw ciphertext string, NOT
        the plaintext — use ``resolve_secret`` for secrets.
        """
        ck = self._ckey(key, scope, scope_id)
        entry = self._config.get(ck)
        if entry is None and self._store is not None:
            entry = self._store.get_config(key, scope, scope_id)
            if entry is not None:
                self._config[ck] = entry
        if entry is None or entry.deleted:
            return default
        return entry.value

    async def delete(
        self,
        key: str,
        scope: ConfigScope = ConfigScope.GLOBAL,
        scope_id: str | None = None,
        changed_by: str = "system",
    ) -> bool:
        """Soft-delete a config key (mark deleted); emit ConfigChanged."""
        ck = self._ckey(key, scope, scope_id)
        entry = self._config.get(ck)
        if entry is None and self._store is not None:
            entry = self._store.get_config(key, scope, scope_id)
        if entry is None:
            return False
        entry.deleted = True
        entry.version += 1
        entry.updated_at = self._clock()
        self._config[ck] = entry
        if self._store is not None:
            self._store.delete_config(key, scope, scope_id)
        await self._emit(ConfigChanged(scope.value, scope_id, key, entry.version, changed_by))
        return True

    def list_keys(
        self,
        scope: ConfigScope | None = None,
        scope_id: str | None = None,
        include_deleted: bool = False,
    ) -> list[str]:
        """List config keys, optionally filtered by scope/scope_id."""
        entries: dict[str, ConfigEntry] = dict(self._config)
        if self._store is not None:
            for e in self._store.list_config(scope, scope_id, include_deleted=True):
                entries[self._ckey(e.key, e.scope, e.scope_id)] = e
        keys: list[str] = []
        for e in entries.values():
            if scope is not None and e.scope != scope:
                continue
            if scope is not None and (scope_id or None) != (e.scope_id or None):
                continue
            if e.deleted and not include_deleted:
                continue
            keys.append(e.key)
        return sorted(set(keys))

    # -- secrets --------------------------------------------------------- #
    async def set_secret(
        self,
        key: str,
        value: str,
        scope: ConfigScope = ConfigScope.GLOBAL,
        scope_id: str | None = None,
        changed_by: str = "system",
    ) -> SecretRef:
        """Encrypt + store a secret (requires a wired cipher). Emits ConfigChanged.

        Also writes a companion ``ConfigEntry(encrypted=True)`` marker so
        ``get``/``list_keys`` surface the key (as raw ciphertext) while
        ``resolve_secret`` remains the only decrypt path.
        """
        if self._cipher is None:
            raise RuntimeError("ConfigVault has no cipher wired; cannot store secrets")
        ck = self._ckey(key, scope, scope_id)
        existing = self._secrets.get(ck)
        if existing is None and self._store is not None:
            existing = self._store.get_secret(key, scope, scope_id)
        version = (existing.version + 1) if existing is not None else 1
        ciphertext = await self._cipher.encrypt(value)
        nonce = getattr(self._cipher, "last_nonce", b"") or b""
        tag = getattr(self._cipher, "last_tag", b"") or b""
        secret = SecretValue(
            key=key,
            scope=scope,
            scope_id=scope_id,
            ciphertext=ciphertext,
            nonce=nonce,
            tag=tag,
            version=version,
            rotated_at=self._clock(),
        )
        self._secrets[ck] = secret
        # marker config entry (encrypted=True) holds the ciphertext (base64 str)
        marker = ConfigEntry(
            key=key,
            value=base64.b64encode(ciphertext).decode("ascii"),
            scope=scope,
            scope_id=scope_id,
            version=version,
            updated_at=self._clock(),
            encrypted=True,
        )
        self._config[ck] = marker
        if self._store is not None:
            self._store.put_secret(secret)
            self._store.put_config(marker)
        await self._emit(ConfigChanged(scope.value, scope_id, key, version, changed_by))
        return SecretRef(key=key, scope=scope, scope_id=scope_id)

    async def resolve_secret(
        self,
        key: str,
        scope: ConfigScope = ConfigScope.GLOBAL,
        scope_id: str | None = None,
        accessor: str = "unknown",
    ) -> str:
        """Decrypt + return a secret's plaintext; write audit (SecretAccessed).

        Raises ``RuntimeError`` when no cipher is wired, ``KeyError`` when the
        secret does not exist.
        """
        if self._cipher is None:
            raise RuntimeError("ConfigVault has no cipher wired; cannot resolve secrets")
        ck = self._ckey(key, scope, scope_id)
        secret = self._secrets.get(ck)
        if secret is None and self._store is not None:
            secret = self._store.get_secret(key, scope, scope_id)
            if secret is not None:
                self._secrets[ck] = secret
        if secret is None:
            await self._record_denied(accessor, key, scope, "secret_not_found")
            raise KeyError(f"secret '{key}' not found in scope {scope.value}:{scope_id or 'global'}")
        plaintext = await self._cipher.decrypt(secret.ciphertext)
        await self._audit_access(key, accessor, "resolve")
        return plaintext

    async def rotate_secret(
        self,
        key: str,
        scope: ConfigScope,
        scope_id: str | None,
        new_value: str,
        rotated_by: str = "system",
    ) -> SecretValue:
        """Re-encrypt a secret with a new value; bump version; emit SecretRotated."""
        if self._cipher is None:
            raise RuntimeError("ConfigVault has no cipher wired; cannot rotate secrets")
        ck = self._ckey(key, scope, scope_id)
        existing = self._secrets.get(ck)
        if existing is None and self._store is not None:
            existing = self._store.get_secret(key, scope, scope_id)
        version = (existing.version + 1) if existing is not None else 1
        ciphertext = await self._cipher.encrypt(new_value)
        rotated_at = self._clock()
        secret = SecretValue(
            key=key,
            scope=scope,
            scope_id=scope_id,
            ciphertext=ciphertext,
            nonce=getattr(self._cipher, "last_nonce", b"") or b"",
            tag=getattr(self._cipher, "last_tag", b"") or b"",
            version=version,
            rotated_at=rotated_at,
        )
        self._secrets[ck] = secret
        marker = ConfigEntry(
            key=key,
            value=base64.b64encode(ciphertext).decode("ascii"),
            scope=scope,
            scope_id=scope_id,
            version=version,
            updated_at=rotated_at,
            encrypted=True,
        )
        self._config[ck] = marker
        if self._store is not None:
            self._store.rotate_secret(secret)
            self._store.put_config(marker)
        await self._emit(
            SecretRotated(key, scope.value, scope_id, rotated_at.isoformat(), version)
        )
        await self._audit_access(key, rotated_by, "read")
        return secret

    # -- reload / audit -------------------------------------------------- #
    async def reload(self, source: str = "manual") -> int:
        """Re-read config + secrets from the store into cache; emit ConfigReloaded.

        Returns the number of config keys reloaded. With no store wired this is
        a no-op over the in-memory cache (returns current size).
        """
        count = 0
        if self._store is not None:
            self._config.clear()
            self._secrets.clear()
            for e in self._store.list_config(None, None, include_deleted=True):
                self._config[self._ckey(e.key, e.scope, e.scope_id)] = e
                count += 1
            for s in self._store.list_secrets():
                self._secrets[self._ckey(s.key, s.scope, s.scope_id)] = s
        else:
            count = len(self._config)
        await self._emit(ConfigReloaded(count, source))
        return count

    def get_audit_log(
        self,
        key: str | None = None,
        accessor: str | None = None,
        since: datetime | None = None,
    ) -> list[AuditEntry]:
        """Return audit entries, filtered by key / accessor / since (proxy to store)."""
        if self._store is not None:
            return self._store.list_audit(key=key, accessor=accessor, since=since)
        entries = self._audit
        out: list[AuditEntry] = []
        for e in entries:
            if key is not None and e.resource != f"secret:{key}":
                continue
            if accessor is not None and e.who != accessor:
                continue
            if since is not None and e.timestamp < since:
                continue
            out.append(e)
        return out

    # -- internal audit helpers ----------------------------------------- #
    async def _audit_access(self, key: str, accessor: str, action: str) -> None:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex,
            who=accessor,
            action=action,
            resource=f"secret:{key}",
            result="allowed",
            timestamp=self._clock(),
        )
        self._audit.append(entry)
        if self._store is not None:
            self._store.put_audit(entry)
        await self._emit(SecretAccessed(key, accessor, action, entry.timestamp.isoformat()))

    async def _record_denied(
        self, principal: str, key: str, scope: ConfigScope, reason: str
    ) -> None:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex,
            who=principal,
            action="resolve",
            resource=f"secret:{key}",
            result="denied",
            timestamp=self._clock(),
            detail=reason,
        )
        self._audit.append(entry)
        if self._store is not None:
            self._store.put_audit(entry)
        await self._emit(ConfigAccessDenied(principal, key, scope.value, reason))


__all__ = ["ConfigVault", "_Base64Cipher"]
