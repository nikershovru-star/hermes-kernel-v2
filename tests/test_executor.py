"""tests/test_executor.py — Task execution end-to-end via EventBus + Capability."""

import asyncio

import pytest

from kernel import domain, registry
from kernel.bus import EventBus
from kernel.capability import CapabilityRegistry
from kernel.executor import (
    EVENT_TASK_COMPLETED,
    EVENT_TASK_FAILED,
    EVENT_TASK_STARTED,
    Executor,
)


async def _build() -> tuple[EventBus, Executor, CapabilityRegistry, registry.ToolRegistry]:
    bus = EventBus()
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    return bus, Executor(bus, cr, tr), cr, tr


async def test_submit_runs_handler_and_emits_events() -> None:
    bus, ex, _, _ = await _build()
    seen = []

    async def on_started(e):
        seen.append(e.type)

    async def on_completed(e):
        seen.append(e.type)

    bus.subscribe(EVENT_TASK_STARTED, on_started)
    bus.subscribe(EVENT_TASK_COMPLETED, on_completed)
    fut = bus.wait_for([EVENT_TASK_COMPLETED])

    ran = {}

    async def handler(task, ctx):
        ran["task"] = task.id
        return "ok"

    ex.register_handler("hermes.echo", handler)
    task = domain.Task(name="t1", capability="hermes.echo")
    result = await ex.submit(task)
    await asyncio.wait_for(fut, timeout=2.0)  # wait for event delivery
    assert result == "ok"
    assert task.status == "COMPLETED"
    assert seen == [EVENT_TASK_STARTED, EVENT_TASK_COMPLETED]
    assert ran["task"] == task.id


async def test_state_machine_status_transitions() -> None:
    bus, ex, _, _ = await _build()
    order = []

    async def handler(task, ctx):
        order.append(task.status)  # should be RUNNING inside handler
        return None

    ex.register_handler("cap.x", handler)
    task = domain.Task(name="t", capability="cap.x")
    await ex.submit(task)
    # QUEUED set before run, RUNNING inside handler, COMPLETED after
    assert order == ["RUNNING"]
    assert task.status == "COMPLETED"


async def test_failure_isolated_and_status_failed() -> None:
    bus, ex, _, _ = await _build()
    failed = []

    async def handler(task, ctx):
        raise RuntimeError("boom")

    async def on_fail(e):
        failed.append(e)

    ex.register_handler("cap.err", handler)
    bus.subscribe(EVENT_TASK_FAILED, on_fail)
    fut = bus.wait_for([EVENT_TASK_FAILED])

    task = domain.Task(name="bad", capability="cap.err")
    with pytest.raises(RuntimeError):
        await ex.submit(task)
    assert task.status == "FAILED"
    evt = await asyncio.wait_for(fut, timeout=2.0)  # wait for delivery
    assert "boom" in evt.payload["error"]


async def test_capability_and_tools_binding_in_context() -> None:
    bus, ex, cr, tr = await _build()
    tool = domain.Tool(name="pdf", capability="hermes.search", input_schema={})
    await tr.register(tool)
    cap = domain.Capability(name="hermes.search", tools=["pdf"])
    await cr.register(cap)

    ctx_seen = {}

    async def handler(task, ctx):
        ctx_seen["cap"] = ctx.get("capability")
        ctx_seen["tools"] = ctx.get("tools")
        return len(ctx["tools"])

    ex.register_handler("hermes.search", handler)
    task = domain.Task(name="s", capability="hermes.search")
    n = await ex.submit(task)
    assert n == 1
    assert ctx_seen["cap"].name == "hermes.search"
    assert [t.name for t in ctx_seen["tools"]] == ["pdf"]


async def test_wait_for_completion_via_sync_barrier() -> None:
    bus, ex, _, _ = await _build()

    async def handler(task, ctx):
        await asyncio.sleep(0.01)
        return "done"

    ex.register_handler("cap.wait", handler)
    task = domain.Task(name="w", capability="cap.wait")
    fut = bus.wait_for([EVENT_TASK_COMPLETED])
    await ex.submit(task)
    evt = await asyncio.wait_for(fut, timeout=2.0)
    assert evt.payload["task_id"] == task.id


async def test_missing_handler_raises_keyerror() -> None:
    bus, ex, _, _ = await _build()
    task = domain.Task(name="x", capability="no.such.cap")
    with pytest.raises(KeyError):
        await ex.submit(task)
