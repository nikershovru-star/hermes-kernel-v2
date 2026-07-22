"""tests/test_mcp_client.py — MCPClient stdio integration (mock server)."""

import asyncio
import sys
from pathlib import Path

import pytest

from kernel import registry
from kernel.bus import EventBus
from mcp.client import EVENT_DISCONNECTED, MCPClient

SERVER = Path(__file__).parent / "fixtures" / "mock_mcp_server.py"


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def tools() -> registry.ToolRegistry:
    return registry.ToolRegistry()


async def test_connect_initialize(bus: EventBus, tools: registry.ToolRegistry) -> None:
    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    info = await client.initialize()
    assert info["serverInfo"]["name"] == "mock-mcp"
    assert client.connected is True
    await client.disconnect()


async def test_tools_list_imports_to_registry(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    await client.initialize()
    imported = await client.tools_list()
    assert {t.name for t in imported} == {"echo", "add"}
    assert (await tools.get_by_name("echo")) is not None
    assert (await tools.get_by_name("add")) is not None
    await client.disconnect()


async def test_tools_call_success(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    await client.initialize()
    res = await client.tools_call("echo", {"text": "hello"})
    assert "result" in res
    assert res["result"]["content"][0]["text"] == "hello"
    await client.disconnect()


async def test_disconnect_graceful(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    await client.initialize()
    fut = bus.wait_for([EVENT_DISCONNECTED])
    await client.disconnect()
    evt = await asyncio.wait_for(fut, timeout=2.0)
    assert evt.type == EVENT_DISCONNECTED
    assert client.connected is False
    assert client._proc.returncode is not None


async def test_tool_adapter_skips_invalid() -> None:
    from mcp.client import MCPToolAdapter

    adapter = MCPToolAdapter()
    # missing name -> None
    assert adapter.to_kernel_tool({"description": "x"}) is None
    # valid -> Tool with mcp namespace capability
    tool = adapter.to_kernel_tool({"name": "foo", "inputSchema": {"type": "object"}})
    assert tool is not None
    assert tool.capability == "mcp.foo"


async def test_rpc_raises_when_not_connected(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    from mcp.client import MCPClientError

    client = MCPClient(bus, tools)
    with pytest.raises(MCPClientError):
        await client.initialize()


async def test_tools_call_error_branch(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    from mcp.client import MCPClientError

    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    await client.initialize()
    # unknown tool -> JSON-RPC error -> MCPClientError raised
    with pytest.raises(MCPClientError):
        await client.tools_call("does_not_exist", {})
    await client.disconnect()


async def test_connect_twice_raises(
    bus: EventBus, tools: registry.ToolRegistry
) -> None:
    from mcp.client import MCPClientError

    client = MCPClient(bus, tools)
    await client.connect([sys.executable, str(SERVER)])
    try:
        with pytest.raises(MCPClientError):
            await client.connect([sys.executable, str(SERVER)])
    finally:
        await client.disconnect()
