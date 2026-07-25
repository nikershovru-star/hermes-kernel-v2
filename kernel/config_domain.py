"""kernel/config_domain.py — Configuration & Secrets domain models (ADR-030).

Isolated from ``kernel.domain`` on purpose (mirrors ``security_domain.py`` /
``observability_domain.py`` / ``marketplace_domain.py``): all ADR-030 config &
secret models live here, axis-clean — stdlib ``datetime`` + ``pydantic`` only.
This keeps ``kernel.config_vault`` / ``kernel.config_store`` axis contracts
minimal (they import only ``kernel.config_domain`` + ``kernel.events``
+ ``kernel.security_domain`` for the shared ``AuditEntry``).

Honest limitations (see ADR-030):
  * Encryption at rest is AES-256-GCM / Fernet via an INJECTABLE cipher — not
    an HSM/KMS integration. ``SecretValue`` records ciphertext/nonce/tag but the
    cipher itself is pluggable (default: a base64 stub for deterministic tests,
    or Fernet from ``cryptography`` when wired).
  * No runtime memory zeroing — plaintext lives in RAM during ``resolve``.
  * Scope-based access control, NOT full RBAC with roles.

AXIS: this module imports nothing from ``kernel`` (self-contained leaf).
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field


class ConfigScope(str, Enum):
    """Isolation scope for a config entry or secret.

    A ``(scope, scope_id)`` pair namespaces every key so agent A never sees
    agent B's config. ``GLOBAL`` uses ``scope_id=None``.
    """

    GLOBAL = "global"
    AGENT = "agent"
    WORKFLOW = "workflow"
    PLUGIN = "plugin"
    MCP_SERVER = "mcp_server"


class ConfigEntry(BaseModel):
    """A single scope-aware configuration value.

    ``encrypted`` marks a value stored as raw ciphertext (a secret); plain
    ``get`` returns it as-is (never auto-decrypts) — use
    ``ConfigVault.resolve_secret`` for the plaintext.
    """

    key: str
    value: str
    scope: ConfigScope = ConfigScope.GLOBAL
    scope_id: str | None = None
    version: int = 1
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    encrypted: bool = False
    deleted: bool = False

    def scope_key(self) -> str:
        """Canonical ``scope:scope_id`` aggregate id (``global`` when no id)."""
        return f"{self.scope.value}:{self.scope_id or 'global'}"


class SecretRef(BaseModel):
    """A pointer to a secret (no ciphertext) — safe to log / pass around."""

    key: str
    scope: ConfigScope = ConfigScope.GLOBAL
    scope_id: str | None = None
    rotation_hint: datetime | None = None

    def scope_key(self) -> str:
        return f"{self.scope.value}:{self.scope_id or 'global'}"


class SecretValue(BaseModel):
    """Encrypted-at-rest secret payload.

    ``ciphertext``/``nonce``/``tag`` model AES-256-GCM output. With the default
    base64 stub cipher, ``nonce``/``tag`` are empty and ``ciphertext`` is the
    base64 of the plaintext (deterministic tests). With a real AEAD cipher they
    carry the GCM nonce and auth tag.
    """

    key: str
    scope: ConfigScope = ConfigScope.GLOBAL
    scope_id: str | None = None
    ciphertext: bytes = b""
    nonce: bytes = b""
    tag: bytes = b""
    version: int = 1
    rotated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def scope_key(self) -> str:
        return f"{self.scope.value}:{self.scope_id or 'global'}"


class ConfigChange(BaseModel):
    """An audit-friendly record of a config mutation (set / delete / rotate)."""

    key: str
    old_value: str | None = None
    new_value: str | None = None
    changed_by: str = "system"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


__all__ = [
    "ConfigScope",
    "ConfigEntry",
    "SecretRef",
    "SecretValue",
    "ConfigChange",
]
