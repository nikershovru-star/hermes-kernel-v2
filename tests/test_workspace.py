"""tests/test_workspace.py — WorkspaceRegistry contract."""

import pytest

from kernel import domain
from kernel.workspace import WorkspaceRegistry


async def test_create_get_list() -> None:
    reg = WorkspaceRegistry()
    ws = await reg.create("proj", "owner-1", {"k": "v"})
    assert ws.name == "proj"
    assert (await reg.get(ws.id)).name == "proj"
    assert (await reg.get_by_name("proj")).id == ws.id
    names = {w.name for w in await reg.list()}
    assert "proj" in names


async def test_duplicate_name_raises() -> None:
    reg = WorkspaceRegistry()
    await reg.create("dup", "o")
    with pytest.raises(ValueError):
        await reg.create("dup", "o2")


async def test_update_delete() -> None:
    reg = WorkspaceRegistry()
    ws = await reg.create("ws", "o", {"a": 1})
    updated = await reg.update(ws.id, name="ws2", settings={"b": 2})
    assert updated.name == "ws2"
    assert updated.settings == {"b": 2}
    # update nonexistent -> None
    assert await reg.update("ghost", name="x") is None
    # delete
    assert await reg.delete(ws.id) is True
    assert await reg.get(ws.id) is None
    # delete nonexistent -> False
    assert await reg.delete("ghost") is False


async def test_default_workspace_auto_created() -> None:
    reg = WorkspaceRegistry()
    listed = await reg.list()
    assert [w.name for w in listed] == ["default"]
    # default is reusable
    assert (await reg.get_by_name("default")) is not None


async def test_set_active_returns_previous() -> None:
    reg = WorkspaceRegistry()
    a = await reg.create("a", "o")
    b = await reg.create("b", "o")
    prev = await reg.set_active(b.id)
    assert prev == a.id  # first created ("a") was active initially
    assert await reg.set_active(a.id) == b.id
    with pytest.raises(ValueError):
        await reg.set_active("ghost")
