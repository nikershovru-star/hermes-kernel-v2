"""tests/test_config_vault.py — ConfigVault unit tests (ADR-030).

Deterministic: injectable clock + a base64 stub cipher (or a deterministic mock
cipher). No real crypto, no wall-clock. asyncio_mode = auto (no decorators).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kernel.config_domain import ConfigScope
from kernel.config_store import ConfigStore
from kernel.config_vault import ConfigVault, _Base64Cipher
from kernel.events import EventBus, EventStore


class _FixedClock:
    """Deterministic monotonically-advancing UTC clock."""

    def __init__(self, start: datetime | None = None):
        self._t = start or datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t = self._t + timedelta(seconds=seconds)


class _MockCipher:
    """Deterministic reversible cipher recording nonce/tag (injectable)."""

    last_nonce = b"NONCE"
    last_tag = b"TAG"

    async def encrypt(self, plaintext: str) -> bytes:
        return b"enc:" + plaintext.encode("utf-8")

    async def decrypt(self, ciphertext: bytes) -> str:
        return ciphertext[len(b"enc:"):].decode("utf-8")


def _vault(store=None, cipher=None, bus=None, event_store=None, clock=None):
    return ConfigVault(
        store=store,
        event_bus=bus,
        event_store=event_store,
        clock=clock or _FixedClock(),
        cipher=cipher if cipher is not None else _Base64Cipher(),
    )


# 1 — set/get roundtrip
async def test_set_get_roundtrip():
    v = _vault()
    entry = await v.set("region", "eu-west", scope=ConfigScope.AGENT, scope_id="a1")
    assert entry.version == 1
    assert await v.get("region", scope=ConfigScope.AGENT, scope_id="a1") == "eu-west"


# 2 — get with default
async def test_get_with_default():
    v = _vault()
    assert await v.get("missing", default="fallback") == "fallback"
    assert await v.get("missing") is None


# 3 — scope isolation (agent A does not see agent B's config)
async def test_scope_isolation():
    v = _vault()
    await v.set("k", "A-value", scope=ConfigScope.AGENT, scope_id="A")
    await v.set("k", "B-value", scope=ConfigScope.AGENT, scope_id="B")
    assert await v.get("k", scope=ConfigScope.AGENT, scope_id="A") == "A-value"
    assert await v.get("k", scope=ConfigScope.AGENT, scope_id="B") == "B-value"
    # global scope is separate again
    assert await v.get("k", scope=ConfigScope.GLOBAL, default="none") == "none"


# 4 — resolve_secret decrypts via cipher
async def test_resolve_secret_decrypts():
    v = _vault(cipher=_MockCipher())
    await v.set_secret("token", "s3cr3t", scope=ConfigScope.AGENT, scope_id="a1")
    # get returns raw ciphertext (never plaintext)
    raw = await v.get("token", scope=ConfigScope.AGENT, scope_id="a1")
    assert raw != "s3cr3t"
    # resolve decrypts
    assert await v.resolve_secret("token", scope=ConfigScope.AGENT, scope_id="a1", accessor="a1") == "s3cr3t"


# 5 — resolve_secret writes audit
async def test_resolve_secret_writes_audit():
    v = _vault()
    await v.set_secret("token", "v", scope=ConfigScope.AGENT, scope_id="a1")
    await v.resolve_secret("token", scope=ConfigScope.AGENT, scope_id="a1", accessor="reader")
    log = v.get_audit_log()
    assert any(e.who == "reader" and e.action == "resolve" and e.result == "allowed" for e in log)


# 6 — rotate_secret bumps version + emits event
async def test_rotate_secret_bumps_version_and_emits():
    store = EventStore()
    v = _vault(event_store=store)
    await v.set_secret("token", "old", scope=ConfigScope.GLOBAL)
    rotated = await v.rotate_secret("token", ConfigScope.GLOBAL, None, "new", rotated_by="ops")
    assert rotated.version == 2
    assert await v.resolve_secret("token", scope=ConfigScope.GLOBAL) == "new"
    events = await store.read_stream("token")
    assert any(e.type == "cfg.secret_rotated" for e in events)


# 7 — delete soft + list excludes deleted
async def test_delete_soft_and_list_excludes():
    v = _vault()
    await v.set("k1", "v1", scope=ConfigScope.AGENT, scope_id="a1")
    await v.set("k2", "v2", scope=ConfigScope.AGENT, scope_id="a1")
    assert await v.delete("k1", scope=ConfigScope.AGENT, scope_id="a1") is True
    assert await v.get("k1", scope=ConfigScope.AGENT, scope_id="a1", default="GONE") == "GONE"
    keys = v.list_keys(scope=ConfigScope.AGENT, scope_id="a1")
    assert "k1" not in keys and "k2" in keys
    # include_deleted surfaces it again
    assert "k1" in v.list_keys(scope=ConfigScope.AGENT, scope_id="a1", include_deleted=True)


# 8 — reload refreshes cache from store
async def test_reload_refreshes_cache():
    store = ConfigStore()
    v = _vault(store=store)
    await v.set("k", "v", scope=ConfigScope.GLOBAL)
    await v.set_secret("s", "secret", scope=ConfigScope.GLOBAL)
    count = await v.reload(source="test")
    assert count >= 1
    # after reload cache is repopulated from store
    assert await v.get("k", scope=ConfigScope.GLOBAL) == "v"
    assert await v.resolve_secret("s", scope=ConfigScope.GLOBAL) == "secret"


# 9 — get_audit_log filtered by key/accessor
async def test_get_audit_log_filtered():
    v = _vault()
    await v.set_secret("a", "1", scope=ConfigScope.GLOBAL)
    await v.set_secret("b", "2", scope=ConfigScope.GLOBAL)
    await v.resolve_secret("a", scope=ConfigScope.GLOBAL, accessor="x")
    await v.resolve_secret("b", scope=ConfigScope.GLOBAL, accessor="y")
    assert all(e.resource == "secret:a" for e in v.get_audit_log(key="a"))
    assert all(e.who == "y" for e in v.get_audit_log(accessor="y"))


# 10 — access denied logging (missing secret)
async def test_access_denied_logging():
    bus_events = []

    class _Bus:
        def publish(self, e):
            bus_events.append(e)

    v = _vault(bus=_Bus())
    with pytest.raises(KeyError):
        await v.resolve_secret("nope", scope=ConfigScope.AGENT, scope_id="a1", accessor="intruder")
    assert any(e.type == "cfg.config_access_denied" for e in bus_events)
    denied = [e for e in v.get_audit_log() if e.result == "denied"]
    assert denied and denied[0].who == "intruder"


# 11 — cipher=None -> resolve_secret raises
async def test_no_cipher_resolve_raises():
    v = ConfigVault(cipher=None)
    with pytest.raises(RuntimeError):
        await v.resolve_secret("x")
    # and set_secret also raises without a cipher
    with pytest.raises(RuntimeError):
        await v.set_secret("x", "y")


# 12 — concurrent set (version race, deterministic with clock)
async def test_concurrent_set_versioning():
    clock = _FixedClock()
    v = _vault(clock=clock)
    e1 = await v.set("k", "v1", scope=ConfigScope.GLOBAL)
    clock.advance(1)
    e2 = await v.set("k", "v2", scope=ConfigScope.GLOBAL)
    clock.advance(1)
    e3 = await v.set("k", "v3", scope=ConfigScope.GLOBAL)
    assert [e1.version, e2.version, e3.version] == [1, 2, 3]
    assert await v.get("k", scope=ConfigScope.GLOBAL) == "v3"
    assert e3.updated_at > e1.updated_at


# bonus — ConfigChanged emitted on set
async def test_config_changed_emitted():
    store = EventStore()
    v = _vault(event_store=store)
    await v.set("k", "v", scope=ConfigScope.AGENT, scope_id="a1")
    events = await store.read_stream("agent:a1")
    assert any(e.type == "cfg.config_changed" and e.payload["key"] == "k" for e in events)


# 13 — store-backed fallback paths (cache miss → store lookup) + delete not-found
async def test_store_backed_fallback_paths():
    cs = ConfigStore()
    v = _vault(store=cs)
    await v.set("k", "v", scope=ConfigScope.AGENT, scope_id="a1")
    await v.set_secret("tok", "sec", scope=ConfigScope.AGENT, scope_id="a1")
    # drop the in-memory cache so the next reads must hit the store
    v._config.clear()
    v._secrets.clear()
    assert await v.get("k", scope=ConfigScope.AGENT, scope_id="a1") == "v"  # 155-157
    v._secrets.clear()
    assert await v.resolve_secret("tok", scope=ConfigScope.AGENT, scope_id="a1") == "sec"  # 277-279
    # delete via store fallback (cache empty)
    v._config.clear()
    assert await v.delete("k", scope=ConfigScope.AGENT, scope_id="a1") is True  # 173
    # delete a genuinely missing key returns False
    assert await v.delete("nope", scope=ConfigScope.AGENT, scope_id="a1") is False  # 175
    # get_audit_log proxies to the store
    log = v.get_audit_log()
    assert any(e.action == "resolve" for e in log)


# 14 — rotate + set_secret existing-version resolved from store (cache cleared)
async def test_rotate_and_setsecret_store_version():
    cs = ConfigStore()
    v = _vault(store=cs)
    await v.set_secret("tok", "v1", scope=ConfigScope.GLOBAL)
    v._secrets.clear()  # force set_secret to look up existing version from store
    ref = await v.set_secret("tok", "v2", scope=ConfigScope.GLOBAL)  # 194-195
    assert ref.key == "tok"
    v._secrets.clear()  # force rotate to look up existing version from store
    rotated = await v.rotate_secret("tok", ConfigScope.GLOBAL, None, "v3")  # 297,301,327-328
    assert rotated.version == 3
    assert await v.resolve_secret("tok", scope=ConfigScope.GLOBAL) == "v3"


# 15 — event_store append failure is swallowed (persistence never breaks a call)
async def test_event_store_failure_swallowed():
    class _BrokenStore:
        async def append(self, event):
            raise RuntimeError("disk full")

    v = ConfigVault(cipher=_Base64Cipher(), event_store=_BrokenStore())
    # set + resolve must still succeed despite the broken event store
    await v.set("k", "v", scope=ConfigScope.GLOBAL)
    await v.set_secret("s", "sec", scope=ConfigScope.GLOBAL)
    assert await v.resolve_secret("s", scope=ConfigScope.GLOBAL) == "sec"
    assert await v.get("k", scope=ConfigScope.GLOBAL) == "v"

