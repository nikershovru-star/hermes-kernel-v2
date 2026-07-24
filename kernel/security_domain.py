"""kernel/security_domain.py — Plugin Sandbox & Security domain models (ADR-028).

Isolated from ``kernel.domain`` on purpose: ADR-020 already defines a
``SandboxPolicy`` in ``kernel.domain`` (resource limits: cpu/memory/file/network/
subprocess — a *different* shape, enforced by ``kernel.sandbox.Sandbox``). The
ADR-028 ``SandboxPolicy`` is permission-based (list[Permission] + soft resource
limits) and must not clobber the ADR-020 one. All ADR-028 security models live
here, axis-clean (stdlib ``datetime`` + ``pydantic`` only) — mirroring
``observability_domain.py``.

AXIS: this module imports nothing from ``kernel`` (self-contained leaf).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class Permission(BaseModel):
    """A single capability grant: ``action`` on ``resource``.

    Examples:
        Permission(action="execute", resource="plugin:weather.fetch")
        Permission(action="discover", resource="plugin:weather")
        Permission(action="network", resource="*")
    """

    action: str
    resource: str

    def matches(self, action: str, resource: str) -> bool:
        """True when this permission authorizes ``(action, resource)``.

        Resource ``"*"`` is a wildcard; ``action`` must match exactly.
        """
        return self.action == action and (
            self.resource == resource or self.resource == "*"
        )


class ResourceLimit(BaseModel):
    """Soft / cooperative resource limits for a sandboxed package.

    These are *not* OS-level (no seccomp / cgroup / WASM). ``max_calls`` and
    ``cpu_ms`` are enforced cooperatively by ``CapabilityGuard`` (it counts
    calls and accrues wall-time via an injectable clock). ``mem_mb`` is declared
    but not hard-enforced (documented limitation — soft only).
    """

    cpu_ms: float = 1000.0
    mem_mb: float = 128.0
    max_calls: int = 100


class SandboxPolicy(BaseModel):
    """Permission-based sandbox policy for an installed plugin package (ADR-028).

    Distinct from ``kernel.domain.SandboxPolicy`` (ADR-020). A package carries at
    most one policy; absence of a policy means "no guard registered" (the guard
    treats an unknown package as allowed when unwired, or as deny-by-default when
    a guard is explicitly wired and no policy was registered — see CapabilityGuard).
    """

    permissions: list[Permission] = Field(default_factory=list)
    resource_limits: ResourceLimit = Field(default_factory=ResourceLimit)

    def allows(self, action: str, resource: str) -> bool:
        return any(p.matches(action, resource) for p in self.permissions)


class AuditEntry(BaseModel):
    """An immutable-in-spirit audit record of a guarded decision.

    Persisted to a ring buffer + SQLite by ``SecurityStore`` (not a WORM store —
    documented limitation).
    """

    entry_id: str
    who: str  # principal (package_id / agent_id)
    action: str
    resource: str
    result: str  # "allowed" | "denied" | "error" | "limit_exceeded"
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    detail: str = ""

    def to_event_payload(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "who": self.who,
            "action": self.action,
            "resource": self.resource,
            "result": self.result,
            "timestamp": self.timestamp.isoformat(),
            "detail": self.detail,
        }
