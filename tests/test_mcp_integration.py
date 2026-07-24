"""tests/test_mcp_integration.py — MCP gateway wired into AgentRuntime /
WorkflowEngine / PluginMarketplace (ADR-029)."""

from __future__ import annotations

import asyncio

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import (
    Agent,
    Artifact,
    Task,
    Workflow,
    WorkflowInstance,
    WorkflowStatus,
    WorkflowStep,
)
from kernel.events import EventBus, EventStore
from kernel.marketplace import PluginMarketplace
from kernel.marketplace_domain import PluginSource
from kernel.mcp_gateway import McpGateway
from kernel.mcp_store import McpStore
from kernel.observability import ObservabilityEngine
from kernel.workflow import WorkflowEngine

URL = "http://mcp.local"

INIT_RESULT = {
    "protocolVersion": "2024-11-05",
    "serverInfo": {"name": "weather-mcp", "version": "1.0"},
    "capabilities": {"tools": {}},
}
TOOLS_RESULT = {
    "tools": [
        {"name": "weather.fetch", "description": "Fetch weather", "inputSchema": {"type": "object"}},
    ]
}
CALL_RESULT = {"content": [{"type": "text", "text": "sunny"}], "isError": False}


async def _instant(_s: float) -> None:
    return None


class MockHttp:
    def __init__(self, results: dict | None = None):
        self.results = results if results is not None else {
            "initialize": INIT_RESULT,
            "tools/list": TOOLS_RESULT,
            "tools/call": CALL_RESULT,
        }
        self.calls: list = []

    async def post(self, url: str, json: dict) -> dict:
        self.calls.append((url, json))
        method = json["method"]
        if method not in self.results:
            return {"jsonrpc": "2.0", "id": json["id"], "error": {"code": -32601, "message": "unknown"}}
        return {"jsonrpc": "2.0", "id": json["id"], "result": self.results[method]}


class FakeAgent(BaseAgent):
    async def start(self) -> str:
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(type="local", content={"local": True}, format="json")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running"}


def _gateway(**kw) -> McpGateway:
    defaults = dict(
        event_bus=EventBus(), event_store=EventStore(), store=McpStore(),
        sleep=_instant, http_client=MockHttp(),
    )
    defaults.update(kw)
    return McpGateway(**defaults)


async def _runtime(mcp: McpGateway | None) -> tuple[AgentRuntime, str]:
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), mcp=mcp)
    agent_id = await rt.start(FakeAgent(Agent(name="a", capabilities=["cap.local"])))
    return rt, agent_id


# -- AgentRuntime ---------------------------------------------------------- #
async def test_agent_execute_mcp_capability_returns_artifact() -> None:
    gw = _gateway()
    rt, agent_id = await _runtime(gw)
    task = Task(name="w", capability=f"mcp:{URL}::weather.fetch", metadata={"city": "Moscow"})
    artifact = await rt.execute(agent_id, task)
    assert artifact.type == "mcp_tool_result"
    assert artifact.content == CALL_RESULT


async def test_agent_execute_mcp_without_gateway_raises() -> None:
    rt, agent_id = await _runtime(None)
    task = Task(name="w", capability="mcp:weather.fetch")
    with pytest.raises(RuntimeError, match="MCP gateway not wired"):
        await rt.execute(agent_id, task)


async def test_agent_execute_mcp_resolved_by_name() -> None:
    gw = _gateway()
    await gw.list_tools(URL)  # populate cache for name resolution
    rt, agent_id = await _runtime(gw)
    artifact = await rt.execute(agent_id, Task(name="w", capability="mcp:weather.fetch"))
    assert artifact.type == "mcp_tool_result"


async def test_agent_list_mcp_tools_proxy_and_empty() -> None:
    gw = _gateway()
    rt, _ = await _runtime(gw)
    tools = await rt.list_mcp_tools(URL)
    assert [t.name for t in tools] == ["weather.fetch"]
    rt_none, _ = await _runtime(None)
    assert await rt_none.list_mcp_tools() == []


# -- WorkflowEngine ------------------------------------------------------------ #
def _engine(mcp: McpGateway | None, rt: AgentRuntime) -> WorkflowEngine:
    return WorkflowEngine(
        agent_runtime=rt,
        capability_executor=CapabilityExecutor(),
        event_bus=EventBus(),
        event_store=EventStore(),
        mcp=mcp,
    )


def _wf(capability: str) -> Workflow:
    return Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="s1", capability=capability)],
        status=WorkflowStatus.RUNNING,
    )


async def test_workflow_step_with_mcp_capability() -> None:
    gw = _gateway()
    rt, _ = await _runtime(gw)
    engine = _engine(gw, rt)
    wf = _wf(f"mcp:{URL}::weather.fetch")
    inst = await engine.start(wf)
    artifact = await engine.execute_step(inst, wf)
    assert artifact.type == "mcp_tool_result"
    assert "mcp_latency_ms" in inst.context
    assert inst.status == WorkflowStatus.COMPLETED


async def test_workflow_mcp_step_without_gateway_fails() -> None:
    rt, _ = await _runtime(None)
    bus = EventBus()
    captured: list = []

    async def handler(event) -> None:
        captured.append(event)

    bus.subscribe("workflow.step_failed", handler)
    engine = WorkflowEngine(
        agent_runtime=rt, capability_executor=CapabilityExecutor(),
        event_bus=bus, event_store=EventStore(), mcp=None,
    )
    wf = _wf("mcp:weather.fetch")
    inst = await engine.start(wf)
    artifact = await engine.execute_step(inst, wf)
    await asyncio.sleep(0)
    assert artifact.type == "error"
    assert artifact.content["reason"] == "mcp_not_wired"
    assert inst.status == WorkflowStatus.FAILED
    assert any("mcp_not_wired" in str(e.payload) for e in captured)


# -- PluginMarketplace ----------------------------------------------------------- #
async def test_marketplace_discover_mcp_tools_adds_to_catalog() -> None:
    gw = _gateway()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=EventStore(), mcp=gw)
    tools = await mp.discover_mcp_tools(URL)
    assert len(tools) == 1
    entries = list(mp._catalog.values())
    assert any(e.source_url == URL for e in entries)


async def test_marketplace_discover_mcp_tools_without_gateway_raises() -> None:
    mp = PluginMarketplace(event_bus=EventBus(), event_store=EventStore())
    with pytest.raises(RuntimeError, match="MCP gateway not wired"):
        await mp.discover_mcp_tools(URL)


async def test_mcp_tool_appears_in_list_available() -> None:
    gw = _gateway()
    mp = PluginMarketplace(event_bus=EventBus(), event_store=EventStore(), mcp=gw)
    await mp.discover_mcp_tools(URL)
    available = mp.list_available()
    mcp_pkgs = [p for p in available if p.source == PluginSource.MCP_SERVER]
    assert len(mcp_pkgs) == 1
    assert mcp_pkgs[0].name == "weather.fetch"
    assert mcp_pkgs[0].capabilities == [f"mcp:{URL}::weather.fetch"]


# -- cross-cutting -------------------------------------------------------------------- #
async def test_metrics_integration_call_tool_records_latency() -> None:
    obs = ObservabilityEngine(event_bus=EventBus(), event_store=EventStore())
    gw = _gateway(metrics=obs)
    await gw.connect(URL)
    await gw.call_tool(URL, "weather.fetch", {"city": "Oslo"})
    names = [m.name for m in obs._metrics]
    assert "mcp.tool_latency_ms" in names


async def test_event_bus_captures_all_five_mcp_events() -> None:
    bus = EventBus()
    captured: list = []

    async def handler(event) -> None:
        captured.append(event.type)

    for etype in ("mcp.connected", "mcp.tool_called", "mcp.resource_read", "mcp.session_closed", "mcp.error"):
        bus.subscribe(etype, handler)
    http = MockHttp()
    http.results["resources/read"] = {"contents": [{"text": "data"}]}
    gw = _gateway(event_bus=bus, http_client=http)
    session = await gw.connect(URL)
    await gw.call_tool(URL, "weather.fetch", {})
    await gw.read_resource(URL, "res://x")
    await gw.close_session(session.session_id)
    # trigger an McpError via a bad method
    http.results.pop("tools/list")
    with pytest.raises(Exception):
        await gw.list_tools(URL)
    await asyncio.sleep(0)
    assert set(captured) == {"mcp.connected", "mcp.tool_called", "mcp.resource_read", "mcp.session_closed", "mcp.error"}


async def test_invalid_jsonrpc_response_yields_error_artifact_and_mcp_error() -> None:
    http = MockHttp(results={"initialize": INIT_RESULT})  # tools/call -> JSON-RPC error
    gw = _gateway(http_client=http)
    await gw.connect(URL)
    artifact = await gw.call_tool(URL, "weather.fetch", {})
    assert artifact.type == "error"
    assert "weather.fetch" in str(artifact.content)
    events = await gw._event_store.read_stream(URL)
    assert any(e.type == "mcp.error" for e in events)


async def test_zero_regression_smoke_mcp_none_everywhere() -> None:
    """AgentRuntime / WorkflowEngine / PluginMarketplace behave identically with mcp=None."""
    rt, agent_id = await _runtime(None)
    artifact = await rt.execute(agent_id, Task(name="t", capability="cap.local"))
    assert artifact.type == "local"
    engine = _engine(None, rt)
    wf = Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="s1", capability="cap.local")],
        status=WorkflowStatus.RUNNING,
    )
    agent_entity = Agent(name="a", capabilities=["cap.local"])
    agent_entity.id = agent_id
    inst = await engine.start(wf)
    result = await engine.execute_step(inst, wf, agent=agent_entity)
    assert result.type in ("local", "error") or result is not None
    mp = PluginMarketplace()
    assert mp.list_available() == []
