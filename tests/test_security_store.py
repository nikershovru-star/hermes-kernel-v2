"""Tests for SecurityStore (ADR-028)."""

from __future__ import annotations

import os
import tempfile

from kernel.security_domain import AuditEntry, Permission, ResourceLimit, SandboxPolicy
from kernel.security_store import SecurityStore


def _tmp_db():
    return tempfile.mktemp(suffix=".db")


def test_put_get_policy_in_memory():
    st = SecurityStore()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:x")])
    st.put_policy("pkg", pol)
    got = st.get_policy("pkg")
    assert got is not None and got.allows("execute", "plugin:x")


def test_policy_sqlite_roundtrip():
    db = _tmp_db()
    try:
        st = SecurityStore(db_path=db)
        pol = SandboxPolicy(
            permissions=[Permission(action="execute", resource="plugin:x")],
            resource_limits=ResourceLimit(cpu_ms=250, max_calls=5),
        )
        st.put_policy("pkg", pol)
        st2 = SecurityStore(db_path=db)
        got = st2.get_policy("pkg")
        assert got is not None
        assert got.allows("execute", "plugin:x")
        assert got.resource_limits.cpu_ms == 250
        assert got.resource_limits.max_calls == 5
    finally:
        st.close()
        st2.close()
        if os.path.exists(db):
            os.remove(db)


def test_grant_roundtrip():
    db = _tmp_db()
    try:
        st = SecurityStore(db_path=db)
        st.put_grant("pkg", Permission(action="discover", resource="plugin:x"))
        st2 = SecurityStore(db_path=db)
        grants = st2.get_grant("pkg")
        assert len(grants) == 1
        assert grants[0].action == "discover"
    finally:
        st.close()
        st2.close()
        if os.path.exists(db):
            os.remove(db)


def test_audit_put_and_list():
    st = SecurityStore()
    entry = AuditEntry(
        entry_id="e1", who="pkg", action="execute", resource="plugin:x", result="allowed"
    )
    st.put_audit(entry)
    log = st.list_audit(principal="pkg")
    assert len(log) == 1 and log[0].result == "allowed"


def test_audit_db_persist():
    db = _tmp_db()
    try:
        st = SecurityStore(db_path=db)
        st.put_audit(AuditEntry(entry_id="e1", who="pkg", action="execute", resource="plugin:x", result="allowed"))
        st2 = SecurityStore(db_path=db)
        log = st2.list_audit(principal="pkg")
        assert len(log) == 1
    finally:
        st.close()
        st2.close()
        if os.path.exists(db):
            os.remove(db)


def test_list_audit_since_until():
    from datetime import datetime, timezone, timedelta

    st = SecurityStore()
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    st.put_audit(AuditEntry(entry_id="e1", who="pkg", action="a", resource="r", result="allowed", timestamp=base))
    st.put_audit(AuditEntry(entry_id="e2", who="pkg", action="a", resource="r", result="allowed", timestamp=base + timedelta(days=2)))
    mid = base + timedelta(days=1)
    after = st.list_audit(since=mid)
    assert len(after) == 1
    before = st.list_audit(until=mid)
    assert len(before) == 1


def test_in_memory_fallback_no_db():
    st = SecurityStore()
    pol = SandboxPolicy(permissions=[Permission(action="execute", resource="*")])
    st.put_policy("pkg", pol)
    st.put_grant("pkg", Permission(action="discover", resource="*"))
    st.put_audit(AuditEntry(entry_id="e1", who="pkg", action="execute", resource="x", result="allowed"))
    assert st.get_policy("pkg") is not None
    assert len(st.get_grant("pkg")) == 1
    assert len(st.list_audit(principal="pkg")) == 1


def test_multiple_packages_isolated():
    st = SecurityStore()
    st.put_policy("a", SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:a")]))
    st.put_policy("b", SandboxPolicy(permissions=[Permission(action="execute", resource="plugin:b")]))
    assert st.get_policy("a").allows("execute", "plugin:a")
    assert not st.get_policy("a").allows("execute", "plugin:b")
    assert st.get_policy("b").allows("execute", "plugin:b")
