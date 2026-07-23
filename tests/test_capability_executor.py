"""tests/test_capability_executor.py — unified CapabilityExecutor (ADR-016)."""

from __future__ import annotations

import pytest

from kernel.capability import CapabilityExecutor
from kernel.domain import Artifact


@pytest.mark.asyncio
async def test_execute_returns_artifact_from_dict() -> None:
    async def handler(params: dict, context: dict | None) -> dict:
        return {"type": "screenshot", "content": "base64img", "format": "png"}

    ex = CapabilityExecutor({"desktop.screenshot": handler})
    art = await ex.execute("desktop.screenshot", {"region": [0, 0, 10, 10]})
    assert isinstance(art, Artifact)
    assert art.type == "screenshot"
    assert art.format == "png"
    assert art.content == "base64img"
    assert "cap:desktop.screenshot" in art.provenance


@pytest.mark.asyncio
async def test_execute_passes_returns_artifact_as_is() -> None:
    base = Artifact(type="text", content="hi", provenance=["task:1"])

    async def handler(params: dict, context: dict | None) -> Artifact:
        return base

    ex = CapabilityExecutor({"echo": handler})
    art = await ex.execute("echo", {})
    assert art is base
    assert "cap:echo" in art.provenance


@pytest.mark.asyncio
async def test_execute_wraps_scalar_result() -> None:
    async def handler(params: dict, context: dict | None) -> str:
        return "plain"

    ex = CapabilityExecutor({"noop": handler})
    art = await ex.execute("noop", {})
    assert art.type == "result"
    assert art.content == "plain"


@pytest.mark.asyncio
async def test_execute_unknown_capability_raises() -> None:
    ex = CapabilityExecutor()
    with pytest.raises(KeyError):
        await ex.execute("missing.cap", {})


@pytest.mark.asyncio
async def test_register_handler_override() -> None:
    async def first(params: dict, context: dict | None) -> str:
        return "a"

    async def second(params: dict, context: dict | None) -> str:
        return "b"

    ex = CapabilityExecutor({"cap": first})
    assert (await ex.execute("cap", {})).content == "a"
    ex.register_handler("cap", second)
    assert (await ex.execute("cap", {})).content == "b"


@pytest.mark.asyncio
async def test_context_passed_through() -> None:
    captured: dict | None = None

    async def handler(params: dict, context: dict | None) -> dict:
        nonlocal captured
        captured = context
        return {"type": "text", "content": "ok"}

    ex = CapabilityExecutor({"x": handler})
    await ex.execute("x", {}, context={"workspace_id": "ws1"})
    assert captured == {"workspace_id": "ws1"}
