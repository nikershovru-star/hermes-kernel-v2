"""tests/test_desktop_agent.py — DesktopAgent (BaseAgent) event-driven lifecycle (ADR-017).

pyautogui is mocked; we verify the agent starts/stops, routes a task through the
CommandBus, emits DomainEvents (screenshot/click), and returns a provenance-
carrying Artifact. EventBus + EventStore are real (in-memory).
"""

from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernel.bus import EventBus
from kernel.domain import Agent, Task
from kernel.events import CommandBus, DomainEvent, EventStore
from plugins.builtin.desktop_control.desktop_agent import DesktopAgent


def _make_agent() -> tuple[DesktopAgent, EventBus, EventStore, CommandBus]:
    bus = EventBus()
    store = EventStore()
    cbus = CommandBus(bus, store)
    entity = Agent(name="desktop", capabilities=["desktop.click", "desktop.type", "desktop.screenshot"])
    agent = DesktopAgent(entity, bus, store, cbus)
    return agent, bus, store, cbus


@pytest.mark.asyncio
async def test_desktop_agent_lifecycle_emits_start_stop() -> None:
    agent, bus, store, _ = _make_agent()
    received: list[DomainEvent] = []

    async def _on_start(e):
        received.append(e)

    async def _on_stop(e):
        received.append(e)

    bus.subscribe("agent.started", _on_start)
    bus.subscribe("agent.stopped", _on_stop)

    aid = await agent.start()
    assert aid == agent.agent_id
    assert agent._running is True
    await agent.stop(aid)
    assert agent._running is False
    await asyncio.sleep(0.02)  # let the bus deliver (fire-and-forget tasks)
    # both events published + stored
    types = [e.type for e in received]
    assert "agent.started" in types and "agent.stopped" in types
    assert store.count() == 2


@pytest.mark.asyncio
async def test_desktop_agent_click_routes_through_command_bus() -> None:
    agent, bus, store, _ = _make_agent()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.desktop_agent._require_pyautogui", return_value=fake_pg):
        task = Task(name="click", capability="desktop.click", metadata={"x": 5, "y": 5})
        art = await agent.execute(agent.agent_id, task)
    fake_pg.click.assert_called_once_with(x=5, y=5, button="left")
    assert art.type == "desktop.click"
    # click event was emitted + stored
    clicks = [e for e in await store.read_stream(agent.agent_id) if e.type == "desktop.clicked"]
    assert len(clicks) == 1
    assert clicks[0].payload["x"] == 5


@pytest.mark.asyncio
async def test_desktop_agent_screenshot_returns_artifact_with_provenance() -> None:
    agent, bus, store, _ = _make_agent()
    await agent.start()
    fake_pg = MagicMock()
    fake_img = MagicMock()
    fake_img.save = MagicMock()
    fake_pg.screenshot.return_value = fake_img
    with patch("plugins.builtin.desktop_control.desktop_agent._require_pyautogui", return_value=fake_pg):
        task = Task(name="shot", capability="desktop.screenshot", metadata={})
        art = await agent.execute(agent.agent_id, task)
    assert art.type == "screenshot"
    assert art.format == "png"
    assert art.content is not None  # screenshot produced an artifact (mocked save)
    assert art.provenance  # at least one event id chained


@pytest.mark.asyncio
async def test_desktop_agent_execute_before_start_raises() -> None:
    agent, _, _, _ = _make_agent()
    task = Task(name="x", capability="desktop.click", metadata={"x": 0, "y": 0})
    with pytest.raises(RuntimeError):
        await agent.execute(agent.agent_id, task)


@pytest.mark.asyncio
async def test_desktop_agent_registered_in_capability_executor() -> None:
    from kernel.capability import CapabilityExecutor

    agent, bus, store, _ = _make_agent()
    await agent.start()
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    # the agent's capabilities are now wired as handlers
    for cap in agent.capabilities:
        assert cap in ex._handlers
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.desktop_agent._require_pyautogui", return_value=fake_pg):
        art = await ex.execute("desktop.click", {"x": 1, "y": 2})
    assert art.type == "desktop.click"


@pytest.mark.asyncio
async def test_desktop_agent_type_task_routes_through_command() -> None:
    agent, bus, store, _ = _make_agent()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.desktop_agent._require_pyautogui", return_value=fake_pg):
        task = Task(name="type", capability="desktop.type", metadata={"text": "hello"})
        art = await agent.execute(agent.agent_id, task)
    fake_pg.write.assert_called_once_with("hello")
    assert art.type == "desktop.type"


@pytest.mark.asyncio
async def test_desktop_agent_via_agent_runtime() -> None:
    from kernel.agent import AgentRuntime

    bus = EventBus()
    store = EventStore()
    cbus = CommandBus(bus, store)
    runtime = AgentRuntime(bus=bus, store=store)
    entity = Agent(name="desktop", capabilities=["desktop.click", "desktop.type", "desktop.screenshot"])
    agent = DesktopAgent(entity, bus, store, cbus)
    aid = await runtime.start(agent)
    assert aid in runtime.list()
    # AgentStarted event emitted via runtime._publish
    started = [e for e in await store.read_all() if e.type == "agent.started"]
    assert any(e.aggregate_id == aid for e in started)
    await runtime.stop(aid)
    assert aid not in runtime.list()
