"""tests/test_registry.py — PluginRegistry + ToolRegistry."""

import pytest

from kernel import domain, registry


async def test_plugin_register_get_unregister() -> None:
    pr = registry.PluginRegistry()
    m = registry.PluginManifest(
        name="obs", version="1.0.0",
        capabilities=["hermes.search"], entrypoint="x:y",
    )
    pid = await pr.register(m, instance=object())
    assert pid == "obs"
    assert await pr.get("obs") is not None
    assert await pr.unregister("obs") is True
    assert await pr.get("obs") is None


async def test_plugin_duplicate_raises() -> None:
    pr = registry.PluginRegistry()
    m = registry.PluginManifest(name="obs", version="1", capabilities=[], entrypoint="x:y")
    await pr.register(m, object())
    with pytest.raises(ValueError):
        await pr.register(m, object())


async def test_plugin_empty_entrypoint_raises() -> None:
    pr = registry.PluginRegistry()
    m = registry.PluginManifest(name="bad", version="1", capabilities=[], entrypoint="")
    with pytest.raises(ValueError):
        await pr.register(m, object())


async def test_plugin_get_by_capability() -> None:
    pr = registry.PluginRegistry()
    inst = object()
    m = registry.PluginManifest(
        name="obs", version="1", capabilities=["hermes.search", "hermes.graph"], entrypoint="x:y"
    )
    await pr.register(m, inst)
    found = await pr.get_by_capability("hermes.search")
    assert found == [inst]


async def test_plugin_list() -> None:
    pr = registry.PluginRegistry()
    m = registry.PluginManifest(name="obs", version="1", capabilities=[], entrypoint="x:y")
    await pr.register(m, object())
    assert len(await pr.list()) == 1


async def test_tool_register_get_discover_unregister() -> None:
    tr = registry.ToolRegistry()
    t = domain.Tool(name="pdf", capability="hermes.search.pdf", input_schema={})
    tid = await tr.register(t)
    assert tid == t.id
    assert (await tr.get(tid)).capability == "hermes.search.pdf"


async def test_tool_discover_prefix_match() -> None:
    tr = registry.ToolRegistry()
    await tr.register(domain.Tool(name="pdf", capability="hermes.search.pdf", input_schema={}))
    await tr.register(domain.Tool(name="md", capability="hermes.search.md", input_schema={}))
    await tr.register(domain.Tool(name="graph", capability="hermes.graph.query", input_schema={}))
    found = await tr.discover("hermes.search")
    assert {t.name for t in found} == {"pdf", "md"}
    # prefix match: "hermes.graph" discovers "hermes.graph.query"
    assert {t.name for t in await tr.discover("hermes.graph")} == {"graph"}
    # capability with no registered tools -> empty
    assert await tr.discover("hermes.nonexistent") == []


async def test_tool_duplicate_raises() -> None:
    tr = registry.ToolRegistry()
    t = domain.Tool(name="pdf", capability="hermes.search.pdf", input_schema={})
    await tr.register(t)
    with pytest.raises(ValueError):
        await tr.register(t)


async def test_tool_empty_capability_raises() -> None:
    tr = registry.ToolRegistry()
    t = domain.Tool(name="x", capability="", input_schema={})
    with pytest.raises(ValueError):
        await tr.register(t)
