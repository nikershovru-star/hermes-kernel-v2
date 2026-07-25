"""tests/test_config_store.py — ConfigStore persistence tests (ADR-030).

Covers the SQLite backend and the pure in-memory fallback. Uses a tempdir DB
file; the connection is closed via reload(None) before teardown to avoid a
Windows file-lock on cleanup.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from kernel.config_domain import ConfigEntry, ConfigScope, SecretValue
from kernel.config_store import ConfigStore
from kernel.security_domain import AuditEntry


@pytest.fixture()
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    try:
        os.remove(path)
    except (OSError, PermissionError):
        pass


def _entry(key="k", scope=ConfigScope.GLOBAL, scope_id=None, encrypted=False, version=1):
    return ConfigEntry(
        key=key,
        value="v",
        scope=scope,
        scope_id=scope_id,
        version=version,
        updated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        encrypted=encrypted,
    )


def _secret(key="s", scope=ConfigScope.GLOBAL, scope_id=None, ct=b"c1", version=1):
    return SecretValue(
        key=key,
        scope=scope,
        scope_id=scope_id,
        ciphertext=ct,
        nonce=b"n",
        tag=b"t",
        version=version,
        rotated_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
    )


# 1 — SQLite roundtrip config/secret/audit
def test_sqlite_roundtrip(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_config(_entry(key="region", scope=ConfigScope.AGENT, scope_id="a1"))
    st.put_secret(_secret(key="tok", scope=ConfigScope.AGENT, scope_id="a1", ct=b"cipher"))
    st.put_audit(AuditEntry(entry_id="e1", who="a1", action="resolve", resource="secret:tok", result="allowed"))
    got_c = st.get_config("region", ConfigScope.AGENT, "a1")
    got_s = st.get_secret("tok", ConfigScope.AGENT, "a1")
    assert got_c is not None and got_c.value == "v"
    assert got_s is not None and got_s.ciphertext == b"cipher"
    assert len(st.list_audit()) == 1
    st.reload(None)


# 2 — list_config filtered by scope
def test_list_config_filtered_by_scope(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_config(_entry(key="a", scope=ConfigScope.AGENT, scope_id="a1"))
    st.put_config(_entry(key="b", scope=ConfigScope.AGENT, scope_id="a2"))
    st.put_config(_entry(key="c", scope=ConfigScope.GLOBAL))
    a1 = st.list_config(ConfigScope.AGENT, "a1")
    assert [e.key for e in a1] == ["a"]
    assert {e.key for e in st.list_config()} == {"a", "b", "c"}
    st.reload(None)


# 3 — secret rotation overwrites (old ciphertext NOT preserved)
def test_secret_rotation_overwrites(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_secret(_secret(key="tok", ct=b"old", version=1))
    st.rotate_secret(_secret(key="tok", ct=b"new", version=2))
    got = st.get_secret("tok", ConfigScope.GLOBAL, None)
    assert got.ciphertext == b"new" and got.version == 2
    # only one row remains — rotation is an overwrite, not append
    assert len(st.list_secrets()) == 1
    st.reload(None)


# 4 — in-memory fallback (db_path=None)
def test_in_memory_fallback():
    st = ConfigStore()  # no db_path
    st.put_config(_entry(key="k", scope=ConfigScope.PLUGIN, scope_id="p1"))
    st.put_secret(_secret(key="s", scope=ConfigScope.PLUGIN, scope_id="p1"))
    assert st.get_config("k", ConfigScope.PLUGIN, "p1").value == "v"
    assert st.get_secret("s", ConfigScope.PLUGIN, "p1") is not None
    assert st._conn is None  # in-memory path never opened a connection


# 5 — repo-reload on db_path
def test_repo_reload(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_config(_entry(key="persist", scope=ConfigScope.GLOBAL))
    st.reload()  # re-open same path
    assert st.get_config("persist", ConfigScope.GLOBAL, None) is not None
    st.reload(None)


# 6 — list_audit with since/until
def test_list_audit_since_until():
    st = ConfigStore()
    base = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
    for i in range(5):
        st.put_audit(
            AuditEntry(
                entry_id=f"e{i}",
                who="a",
                action="resolve",
                resource="secret:x",
                result="allowed",
                timestamp=base + timedelta(minutes=i),
            )
        )
    since = base + timedelta(minutes=2)
    until = base + timedelta(minutes=3)
    got = st.list_audit(since=since, until=until)
    assert len(got) == 2  # minutes 2 and 3


# 7 — encrypted flag persistence
def test_encrypted_flag_persistence(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_config(_entry(key="plain", encrypted=False))
    st.put_config(_entry(key="secret", encrypted=True))
    st.reload()  # force read from disk
    assert st.get_config("plain", ConfigScope.GLOBAL, None).encrypted is False
    assert st.get_config("secret", ConfigScope.GLOBAL, None).encrypted is True
    st.reload(None)


# 8 — soft delete then restore via put_config
def test_soft_delete_and_restore(db_path):
    st = ConfigStore(db_path=db_path)
    st.put_config(_entry(key="k", scope=ConfigScope.GLOBAL))
    assert st.delete_config("k", ConfigScope.GLOBAL, None) is True
    got = st.get_config("k", ConfigScope.GLOBAL, None)
    assert got.deleted is True and got.version == 2
    # excluded from default list, included with include_deleted
    assert st.list_config(ConfigScope.GLOBAL, None) == []
    assert len(st.list_config(ConfigScope.GLOBAL, None, include_deleted=True)) == 1
    # restore: put a fresh non-deleted entry
    st.put_config(_entry(key="k", scope=ConfigScope.GLOBAL, version=3))
    assert st.get_config("k", ConfigScope.GLOBAL, None).deleted is False
    st.reload(None)
