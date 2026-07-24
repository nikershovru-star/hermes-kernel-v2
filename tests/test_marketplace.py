"""tests/test_marketplace.py — PluginMarketplace (ADR-026).

Deterministic: injectable rng, clock, http_client, sleep; no real network.
"""

from __future__ import annotations

import asyncio
import json
import random

import pytest
from kernel.events import EventBus, EventStore
from kernel.marketplace import PluginMarketplace
from kernel.marketplace_domain import PluginPackage, PluginSource, PluginStatus


def _pkg(pid="pkg.x", name="x", caps=None, checksum=None, deps=None, source=PluginSource.MARKETPLACE, entrypoint="plugins.x:X"):
    return PluginPackage(
        package_id=pid, name=name, version="1.0", source=source, entrypoint=entrypoint,
        capabilities=caps or ["cap.x"], dependencies=deps or [], checksum=checksum, status=PluginStatus.AVAILABLE,
    )


def _mp(**kw):
    return PluginMarketplace(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(5), **kw)


class _MockHTTP:
    def __init__(self, catalog):
        self._catalog = catalog

    async def get(self, url):
        return json.dumps(self._catalog)


async def test_discover_fetches_catalog() -> None:
    http = _MockHTTP([{"name": "vision", "package_id": "pkg.vision", "capabilities": ["img.classify"], "checksum": None}])
    mp = _mp(http_client=http)
    entries = await mp.discover("http://catalog")
    assert len(entries) == 1
    assert len(mp.list_available()) == 1


async def test_discover_requires_http_client() -> None:
    mp = _mp()
    with pytest.raises(RuntimeError):
        await mp.discover("http://catalog")


async def test_install_sets_installed_status() -> None:
    mp = _mp()
    inst = await mp.install(_pkg())
    assert inst.status == PluginStatus.INSTALLED
    assert inst.installed_at is not None
    assert len(mp.list_installed()) == 1


async def test_install_failed_on_checksum_mismatch() -> None:
    mp = _mp()
    bad = _pkg(pid="b", checksum="deadbeef", entrypoint="plugins.b:B")
    inst = await mp.install(bad)
    assert inst.status == PluginStatus.FAILED
    assert len(mp.list_installed()) == 0


async def test_uninstall_removes_package() -> None:
    mp = _mp()
    await mp.install(_pkg())
    assert await mp.uninstall("pkg.x") is True
    assert len(mp.list_installed()) == 0
    assert await mp.uninstall("pkg.x") is False


async def test_validate_package_checksum_ok() -> None:
    import hashlib

    entry = "plugins.x:X"
    digest = hashlib.sha256(entry.encode()).hexdigest()
    mp = _mp()
    ok, reason = mp.validate_package(_pkg(checksum=digest))
    assert ok is True and reason == ""


async def test_validate_package_missing_dependency() -> None:
    mp = _mp()
    ok, reason = mp.validate_package(_pkg(deps=["missing.dep"]))
    assert ok is False
    assert "missing" in reason


async def test_list_available_after_discover() -> None:
    http = _MockHTTP([{"name": "a", "package_id": "p.a", "capabilities": ["cap.a"]}])
    mp = _mp(http_client=http)
    await mp.discover("http://catalog")
    avail = mp.list_available()
    assert any(p.package_id == "p.a" for p in avail)


async def test_install_emits_plugin_installed_event() -> None:
    store = EventStore()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    await mp.install(_pkg())
    assert any(e.type == "mp.plugin_installed" for e in store._events)


async def test_install_failed_emits_event() -> None:
    store = EventStore()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    await mp.install(_pkg(pid="b", checksum="deadbeef", entrypoint="plugins.b:B"))
    assert any(e.type == "mp.plugin_install_failed" for e in store._events)


async def test_register_local_marks_installed() -> None:
    mp = _mp()
    pkg = await mp.register_local("plugins.local:L", ["cap.local"])
    assert pkg.source == PluginSource.LOCAL
    assert pkg.status == PluginStatus.INSTALLED
    assert len(mp.list_installed()) == 1


async def test_injectable_clock_used_for_installed_at() -> None:
    from datetime import datetime, timezone

    fixed = datetime(2026, 1, 1, tzinfo=timezone.utc)
    mp = _mp(clock=lambda: fixed)
    inst = await mp.install(_pkg())
    assert inst.installed_at == fixed
