"""tests/test_capability.py — CapabilityRegistry contract."""

import logging

import pytest

from kernel import domain, registry
from kernel.capability import CapabilityRegistry


async def test_register_get_unregister() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    cap = domain.Capability(name="hermes.search")
    cid = await cr.register(cap)
    assert isinstance(cid, str)
    assert (await cr.get(cid)).name == "hermes.search"
    assert await cr.unregister(cid) is True
    assert await cr.get(cid) is None


async def test_duplicate_name_raises() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    await cr.register(domain.Capability(name="dup"))
    with pytest.raises(ValueError):
        await cr.register(domain.Capability(name="dup"))


async def test_resolve_tools() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    tool = domain.Tool(name="t1", capability="hermes.search", input_schema={})
    await tr.register(tool)
    cap = domain.Capability(name="hermes.search", tools=["t1"])
    cid = await cr.register(cap)
    resolved = await cr.resolve_tools(cid)
    assert resolved == [tool]


async def test_resolve_tools_skips_missing(caplog: pytest.LogCaptureFixture) -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    cap = domain.Capability(name="hermes.search", tools=["ghost"])
    cid = await cr.register(cap)
    with caplog.at_level(logging.WARNING):
        resolved = await cr.resolve_tools(cid)
    assert resolved == []
    assert any("ghost" in r.message for r in caplog.records)


async def test_discover_prefix() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    for n in ["hermes.search", "hermes.graph", "other"]:
        await cr.register(domain.Capability(name=n))
    res = await cr.discover("hermes")
    names = {c.name for c in res}
    assert names == {"hermes.search", "hermes.graph"}
    # empty prefix -> all
    assert len(await cr.discover("")) == 3


async def test_get_by_name_and_missing() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    cap = domain.Capability(name="hermes.search")
    await cr.register(cap)
    assert (await cr.get_by_name("hermes.search")).id == cap.id
    assert await cr.get_by_name("nope") is None


async def test_resolve_tools_unknown_capability() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    assert await cr.resolve_tools("ghost-id") == []


async def test_discover_exact_match() -> None:
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    await cr.register(domain.Capability(name="hermes.search"))
    res = await cr.discover("hermes.search")
    assert [c.name for c in res] == ["hermes.search"]
