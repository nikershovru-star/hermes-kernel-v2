"""Tests for CapabilityGuard (ADR-028)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, timedelta

import pytest

from kernel.capability_guard import (
    CapabilityGuard,
    PermissionDeniedError,
    ResourceLimitExceededError,
)
from kernel.events import EventBus, EventStore
from kernel.security_domain import AuditEntry, Permission, ResourceLimit, SandboxPolicy
from kernel.security_store import SecurityStore


def _guard(bus=None, store=None, event_store=None, clock=None):
    # ``store`` is a SecurityStore (policy/grant/audit persistence); ``event_store``
    # is an EventStore that captures guard-published domain events.
    return CapabilityGuard(
        event_bus=bus,
        event_store=event_store or (EventStore() if bus is not None else None),
        store=store,
        clock=clock or (lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)),
    )


def test_register_and_get_policies():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))
    assert "pkg" in g.get_policies()
    assert g.get_policies()["pkg"].allows("execute", "plugin:x")


def test_check_allow_via_policy():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))
    assert g.check("pkg", "execute", "plugin:x") is True


def test_check_deny_via_policy():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))
    assert g.check("pkg", "delete", "plugin:x") is False
    assert g.check("pkg", "execute", "plugin:other") is False


def test_check_unregistered_package_is_allowed():
    # zero regression: an un-sandboxed package is allowed when no policy exists
    g = _guard()
    assert g.check("unknown", "anything", "plugin:whatever") is True


def test_grant_layering_on_top_of_policy():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))
    asyncio.run(g.grant("pkg", Permission(action="discover", resource="plugin:x")))
    assert g.check("pkg", "discover", "plugin:x") is True


def test_wrap_success():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))
    calls = []

    async def handler():
        calls.append(1)
        return "ok"

    res = asyncio.run(g.call(lambda: handler(), "pkg", action="execute", resource="plugin:x"))
    assert res == "ok"
    assert calls == [1]


def test_wrap_permission_denied():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))

    async def handler():
        return "should-not-run"

    with pytest.raises(PermissionDeniedError):
        asyncio.run(g.call(lambda: handler(), "pkg", action="delete", resource="plugin:x"))


def test_wrap_resource_limit_calls():
    g = _guard()
    pol = SandboxPolicy(
        permissions=[Permission(action="execute", resource="*")],
        resource_limits=ResourceLimit(cpu_ms=1000, max_calls=1),
    )
    asyncio.run(g.register_policy("pkg", pol))

    async def handler():
        return "ok"

    asyncio.run(g.call(lambda: handler(), "pkg"))
    with pytest.raises(ResourceLimitExceededError):
        asyncio.run(g.call(lambda: handler(), "pkg"))


async def test_wrap_resource_limit_cpu():
    # The guard accrues cpu_ms as the delta BETWEEN __aenter__ and __aexit__
    # (wall-time INSIDE one call) and enforces the limit on the NEXT call's
    # pre-check. The injected clock must advance DURING the call, so the
    # handler itself moves the clock forward by 1000ms. Call 1 accrues 0ms
    # (frozen clock), call 2 accrues 1000ms — after which _resource_ok must
    # report the cpu_ms breach and _pre must deny the next call.
    t = {"n": datetime(2026, 1, 1, tzinfo=timezone.utc)}
    g = _guard(clock=lambda: t["n"])
    pol = SandboxPolicy(
        permissions=[Permission(action="execute", resource="*")],
        resource_limits=ResourceLimit(cpu_ms=500, max_calls=100),
    )
    await g.register_policy("pkg", pol)

    async def handler():
        # advance the clock mid-call by 300ms so the call's measured duration
        # is 300ms (under the 500ms limit per-call, but cumulative > 500ms)
        t["n"] = t["n"] + timedelta(milliseconds=300)
        return "ok"

    # call 1: accrues 300ms (allowed — pre-check sees 0 prior usage)
    await g.call(lambda: handler(), "pkg")
    # call 2: accrues another 300ms (allowed — pre-check sees 300 < 500)
    await g.call(lambda: handler(), "pkg")

    # cumulative usage is now 600ms (>500ms limit)
    ok, limit_type = g._resource_ok("pkg")
    assert ok is False and limit_type == "cpu_ms"

    # the next guarded call's pre-check must deny (raise) on the cpu_ms breach
    denied = False
    try:
        await g._pre("pkg", "execute", "plugin:pkg")
    except ResourceLimitExceededError:
        denied = True
    assert denied, "expected ResourceLimitExceededError from _pre after cpu_ms breach"


def test_audit_log_accumulates_allowed_and_denied():
    g = _guard()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))

    async def handler():
        return "ok"

    asyncio.run(g.call(lambda: handler(), "pkg", action="execute", resource="plugin:x"))
    with pytest.raises(PermissionDeniedError):
        asyncio.run(g.call(lambda: handler(), "pkg", action="delete", resource="plugin:x"))
    log = g.get_audit_log(package_id="pkg")
    assert any(e.result == "allowed" for e in log)
    assert any(e.result == "denied" for e in log)


def test_audit_log_since_filter():
    early = datetime(2026, 1, 1, tzinfo=timezone.utc)
    late = datetime(2026, 1, 2, tzinfo=timezone.utc)
    g = _guard(clock=lambda: early)
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="*")])
    asyncio.run(g.register_policy("pkg", pol))

    async def handler():
        return "ok"

    asyncio.run(g.call(lambda: handler(), "pkg"))

    def late_clock():
        return late

    g._clock = late_clock
    asyncio.run(g.call(lambda: handler(), "pkg"))
    after = g.get_audit_log(since=late)
    assert len(after) == 1


def test_events_emitted_on_bus():
    bus = EventBus()
    event_store = EventStore()  # captures guard-published domain events
    sec_store = SecurityStore()  # policy/grant/audit persistence
    g = _guard(bus=bus, store=sec_store, event_store=event_store)
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    asyncio.run(g.register_policy("pkg", pol))

    async def handler():
        return "ok"

    with pytest.raises(PermissionDeniedError):
        asyncio.run(g.call(lambda: handler(), "pkg", action="delete", resource="plugin:x"))
    # audit entry persisted through the guard's own audit log (SecurityStore-backed)
    assert any(e.result == "denied" for e in g.get_audit_log(package_id="pkg"))
    # the guard also publishes on the bus -> captured by EventStore
    types = {e.type for e in asyncio.run(event_store.read_all())}
    assert "sec.plugin_sandboxed" in types
    assert "sec.audit_entry" in types
    assert "sec.permission_denied" in types
