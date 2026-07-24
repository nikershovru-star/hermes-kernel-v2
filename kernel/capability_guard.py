"""kernel/capability_guard.py — CapabilityGuard (ADR-028).

In-process, cooperative plugin sandbox: permission-based access control +
soft resource limits + audit log. All I/O is injectable so it runs with zero
side effects (clock / event_bus / event_store / store / sleep).

AXIS CONTRACT: imports only ``kernel.security_domain`` + ``kernel.events``.
No reverse dependency on workflow / agent / marketplace — those *wire* the
guard in (Stage 4/5/6). Never imports ``plugins`` / ``mcp``.

Honest limitations (documented in ADR-028):
* in-process only — NOT OS-level seccomp / cgroup / WASM isolation.
* resource limits are COOPERATIVE / SOFT — we count calls and accrue cpu_ms via
  the injectable clock; there is no hard OOM-killer or preemption.
* policy "signature" is a basic validation step in PluginMarketplace, not full
  PKI.
* audit log is a ring buffer + SQLite — not an immutable WORM store.
* the guard is OPTIONAL: when not wired (None) the kernel behaves exactly as
  before (full backward compatibility).
"""

from __future__ import annotations

import asyncio
import random
import uuid
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.events import (
    AuditLogEntry,
    EventBus,
    EventStore,
    PermissionDenied,
    PluginSandboxed,
    ResourceLimitExceeded,
)
from kernel.security_domain import (
    AuditEntry,
    Permission,
    ResourceLimit,
    SandboxPolicy,
)


class PermissionDeniedError(Exception):
    """Raised by ``CapabilityGuard.wrap`` when no permission authorizes the action."""


class ResourceLimitExceededError(Exception):
    """Raised by ``CapabilityGuard.wrap`` when a cooperative resource limit is breached."""


class CapabilityGuard:
    """Permission-based, cooperative sandbox for installed plugin packages."""

    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        store: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rng: random.Random | None = None,
        sleep: Callable[..., Awaitable[None]] = asyncio.sleep,
        audit_limit: int = 1000,
    ) -> None:
        self._bus = event_bus
        self._event_store = event_store
        self._store = store
        self._clock = clock
        self._rng = rng or random.Random()
        self._sleep = sleep
        # package_id -> SandboxPolicy
        self._policies: dict[str, SandboxPolicy] = {}
        # package_id -> list[Permission] (grants layered on top of policy)
        self._grants: dict[str, list[Permission]] = {}
        # package_id -> {"calls": int, "cpu_ms": float}
        self._usage: dict[str, dict[str, float]] = {}
        # ring buffer (live audit when no store)
        self._audit: list[AuditEntry] = []
        self._audit_limit = audit_limit

    # -- policy registration -------------------------------------------- #
    async def register_policy(self, package_id: str, policy: SandboxPolicy) -> None:
        self._policies[package_id] = policy
        self._usage.setdefault(package_id, {"calls": 0.0, "cpu_ms": 0.0})
        if self._store is not None:
            self._store.put_policy(package_id, policy)
        summary = f"{len(policy.permissions)} perms / cpu={policy.resource_limits.cpu_ms}ms calls={policy.resource_limits.max_calls}"
        await self._emit(PluginSandboxed(package_id, summary))

    def get_policies(self) -> dict[str, SandboxPolicy]:
        return dict(self._policies)

    # -- grants (layered permissions) ----------------------------------- #
    async def grant(self, package_id: str, permission: Permission) -> None:
        self._grants.setdefault(package_id, []).append(permission)
        if self._store is not None:
            self._store.put_grant(package_id, permission)

    # -- permission check ----------------------------------------------- #
    def check(self, principal: str, action: str, resource: str) -> bool:
        """Return True if ``principal`` may perform ``(action, resource)``.

        * No registered policy + no grants => allow (package not sandboxed).
        * Otherwise allow iff the policy OR a grant matches.
        Resource limits are NOT checked here (enforced cooperatively in ``wrap``).
        """
        policy = self._policies.get(principal)
        if policy is not None and policy.allows(action, resource):
            return True
        grants = self._grants.get(principal)
        if grants and any(p.matches(action, resource) for p in grants):
            return True
        # un-registered principal: not sandboxed -> allow (backward compatible)
        return policy is None and not grants

    # -- audit ----------------------------------------------------------- #
    async def _audit_record(
        self, who: str, action: str, resource: str, result: str, detail: str = ""
    ) -> AuditEntry:
        entry = AuditEntry(
            entry_id=uuid.uuid4().hex,
            who=who,
            action=action,
            resource=resource,
            result=result,
            timestamp=self._clock(),
            detail=detail,
        )
        self._audit.append(entry)
        if len(self._audit) > self._audit_limit:
            self._audit = self._audit[-self._audit_limit:]
        if self._store is not None:
            self._store.put_audit(entry)
        await self._emit(
            AuditLogEntry(
                entry.entry_id,
                who,
                action,
                resource,
                result,
                entry.timestamp.isoformat(),
            )
        )
        return entry

    def get_audit_log(
        self,
        package_id: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[AuditEntry]:
        if self._store is not None:
            return self._store.list_audit(principal=package_id, since=since, until=until)
        out = self._audit
        if package_id is not None:
            out = [e for e in out if e.who == package_id]
        if since is not None:
            out = [e for e in out if e.timestamp >= since]
        if until is not None:
            out = [e for e in out if e.timestamp <= until]
        return out

    # -- cooperative resource pre-check -------------------------------- #
    def _resource_ok(self, package_id: str) -> tuple[bool, str]:
        policy = self._policies.get(package_id)
        if policy is None:
            return True, ""
        usage = self._usage.get(package_id, {"calls": 0.0, "cpu_ms": 0.0})
        rl: ResourceLimit = policy.resource_limits
        if usage["calls"] >= rl.max_calls:
            return False, "max_calls"
        if usage["cpu_ms"] >= rl.cpu_ms:
            return False, "cpu_ms"
        return True, ""

    # -- wrap (async context manager) ----------------------------------- #
    def wrap(
        self,
        handler: Callable[[], Awaitable[Any]],
        package_id: str,
        action: str = "execute",
        resource: str | None = None,
    ) -> "_GuardContext":
        """Cooperatively guard an async call to ``handler()``.

        ``handler`` is a zero-arg callable returning an awaitable. Usage::

            async with guard.wrap(lambda: agent.execute(aid, task), pkg_id,
                                  action="execute", resource="plugin:x"):
                result = await handler_call

        On permission denial / resource breach, raises ``PermissionDeniedError``
        / ``ResourceLimitExceededError`` (audit + events still emitted).
        """
        if resource is None:
            resource = f"plugin:{package_id}"
        return _GuardContext(self, handler, package_id, action, resource)

    # -- direct call helper (convenience for integrations) -------------- #
    async def call(
        self,
        handler: Callable[[], Awaitable[Any]],
        package_id: str,
        action: str = "execute",
        resource: str | None = None,
    ) -> Any:
        async with self.wrap(handler, package_id, action=action, resource=resource) as coro:
            return await coro

    # -- emission -------------------------------------------------------- #
    async def _emit(self, event: Any) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001
                pass

    # -- internal hooks used by _GuardContext --------------------------- #
    async def _pre(self, package_id: str, action: str, resource: str) -> None:
        if not self.check(package_id, action, resource):
            await self._audit_record(package_id, action, resource, "denied")
            await self._emit(PermissionDenied(package_id, action, resource))
            raise PermissionDeniedError(
                f"permission denied: {action} on {resource} for {package_id}"
            )
        ok, limit_type = self._resource_ok(package_id)
        if not ok:
            await self._audit_record(
                package_id, action, resource, "limit_exceeded", detail=limit_type
            )
            await self._emit(ResourceLimitExceeded(package_id, limit_type, 0.0))
            raise ResourceLimitExceededError(
                f"resource limit exceeded ({limit_type}) for {package_id}"
            )

    def _accrue(self, package_id: str, ms: float) -> None:
        usage = self._usage.setdefault(package_id, {"calls": 0.0, "cpu_ms": 0.0})
        usage["calls"] += 1
        usage["cpu_ms"] += ms

    async def _post(self, package_id: str, action: str, resource: str, errored: bool) -> None:
        result = "error" if errored else "allowed"
        await self._audit_record(package_id, action, resource, result)

    def _now(self) -> datetime:
        return self._clock()


class _GuardContext:
    """Async context manager returned by ``CapabilityGuard.wrap``."""

    def __init__(
        self,
        guard: CapabilityGuard,
        handler: Callable[[], Awaitable[Any]],
        package_id: str,
        action: str,
        resource: str,
    ) -> None:
        self._guard = guard
        self._handler = handler
        self._package_id = package_id
        self._action = action
        self._resource = resource
        self._start: datetime | None = None
        self._errored = False

    async def __aenter__(self) -> Awaitable[Any]:
        # permission + resource pre-check (may raise)
        await self._guard._pre(self._package_id, self._action, self._resource)
        self._start = self._guard._now()
        # return a coroutine that runs the handler
        return self._handler()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        end = self._guard._now()
        ms = 0.0
        if self._start is not None:
            ms = max(0.0, (end - self._start).total_seconds() * 1000.0)
        self._guard._accrue(self._package_id, ms)
        self._errored = exc_type is not None
        await self._guard._post(self._package_id, self._action, self._resource, self._errored)
        # do not suppress exceptions from the wrapped call
        return False
