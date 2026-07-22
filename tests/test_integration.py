"""tests/test_integration.py — end-to-end: Workspace + Capability + Tool + Executor + EventBus.

Cross-layer integration proving the FOCUS Phase 1 claim: "Task end-to-end
through EventBus with Capability in the scope of a Workspace". A Task carries
workspace_id; a Capability binds Tools; the Executor resolves them and emits
lifecycle Events that the bus sync-barrier can await.
"""

import asyncio

import pytest

from kernel import domain, registry
from kernel.bus import EventBus
from kernel.capability import CapabilityRegistry
from kernel.executor import EVENT_TASK_COMPLETED, EVENT_TASK_FAILED, Executor
from kernel.workspace import WorkspaceRegistry


async def _build_system() -> tuple[EventBus, Executor, WorkspaceRegistry, CapabilityRegistry, registry.ToolRegistry]:
    bus = EventBus()
    tr = registry.ToolRegistry()
    cr = CapabilityRegistry(tr)
    wsr = WorkspaceRegistry()
    ex = Executor(bus, cr, tr)
    return bus, ex, wsr, cr, tr


async def test_end_to_end_task_with_capability_and_tools() -> None:
    bus, ex, wsr, cr, tr = await _build_system()

    # 1) workspace isolates the work
    ws = await wsr.create("project-x", "owner-1", {"lang": "ru"})

    # 2) register a tool + capability (capability bundles the tool)
    tool = domain.Tool(name="pdf.read", capability="hermes.fs.read", input_schema={})
    await tr.register(tool)
    cap = domain.Capability(name="hermes.fs.read", tools=["pdf.read"])
    await cr.register(cap)

    # 3) executor handler receives capability + resolved tools in ctx
    ctx_seen = {}

    async def handler(task, ctx):
        ctx_seen["ws"] = ctx.get("capability") is not None
        ctx_seen["tools"] = [t.name for t in ctx["tools"]]
        return f"read {len(ctx['tools'])} tool(s) in ws {ws.id[:8]}"

    ex.register_handler("hermes.fs.read", handler)

    # 4) task scoped to the workspace, executing the capability
    task = domain.Task(name="ingest", capability="hermes.fs.read", workspace_id=ws.id)

    # 5) sync barrier: await completion via the bus
    fut = bus.wait_for([EVENT_TASK_COMPLETED])
    result = await ex.submit(task)
    evt = await asyncio.wait_for(fut, timeout=2.0)

    assert result.startswith("read 1 tool")
    assert task.workspace_id == ws.id
    assert task.status == "COMPLETED"
    assert ctx_seen["tools"] == ["pdf.read"]
    assert evt.payload["task_id"] == task.id


async def test_end_to_end_failure_emits_event_and_isolates() -> None:
    bus, ex, wsr, cr, tr = await _build_system()
    await wsr.create("default-ws", "o")

    async def bad(task, ctx):
        raise RuntimeError("kaboom")

    ex.register_handler("cap.fail", bad)
    fut = bus.wait_for([EVENT_TASK_FAILED])

    task = domain.Task(name="boom", capability="cap.fail")
    with pytest.raises(RuntimeError):
        await ex.submit(task)
    evt = await asyncio.wait_for(fut, timeout=2.0)

    assert task.status == "FAILED"
    assert "kaboom" in evt.payload["error"]


async def test_capability_discovery_drives_executor_routing() -> None:
    """Discover capabilities by prefix, then route a task to the first match."""
    bus, ex, wsr, cr, tr = await _build_system()
    for n in ["hermes.search", "hermes.graph", "other"]:
        await cr.register(domain.Capability(name=n, tools=[]))

    discovered = await cr.discover("hermes")
    assert {c.name for c in discovered} == {"hermes.search", "hermes.graph"}

    routed = {}

    async def h(task, ctx):
        routed["cap"] = task.capability
        return True

    ex.register_handler("hermes.search", h)
    task = domain.Task(name="q", capability="hermes.search")
    await ex.submit(task)
    assert routed["cap"] == "hermes.search"
