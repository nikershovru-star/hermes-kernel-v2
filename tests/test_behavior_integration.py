"""tests/test_behavior_integration.py — DesktopAgent + BehaviorEngine (ADR-022).

Verifies DesktopAgent routes task.capability through the BehaviorEngine when one
is wired in, that behavior events land in the EventStore, that the legacy
CommandBus path still works without a BehaviorEngine (backward compat), and that
DesktopVision surfaces element centers for behavior targeting.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import pytest
from kernel.bus import EventBus
from kernel.domain import Agent, BehaviorProfile, Task
from kernel.events import CommandBus, EventStore
from plugins.builtin.desktop_control.behavior import BehaviorEngine
from plugins.builtin.desktop_control.desktop_agent import DesktopAgent
from plugins.builtin.desktop_control.vision import DesktopVision, UIElement


async def _instant(_s: float) -> None:
    return None


def _agent_with_behavior():
    bus = EventBus()
    store = EventStore()
    cbus = CommandBus(bus, store)
    entity = Agent(
        name="desktop",
        capabilities=["desktop.click", "desktop.type", "desktop.scroll", "desktop.read"],
    )
    behavior = BehaviorEngine(
        BehaviorProfile(typing_error_rate=0.0),
        agent_id="beh",
        event_bus=bus,
        event_store=store,
        rng=random.Random(7),
        sleep=_instant,
    )
    agent = DesktopAgent(entity, bus, store, cbus, behavior=behavior)
    return agent, bus, store, behavior


def _agent_legacy():
    bus = EventBus()
    store = EventStore()
    cbus = CommandBus(bus, store)
    entity = Agent(name="desktop", capabilities=["desktop.click"])
    return DesktopAgent(entity, bus, store, cbus), store


# --- Behavior routing ----------------------------------------------------- #
async def test_click_routes_through_behavior() -> None:
    agent, _, store, _ = _agent_with_behavior()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        task = Task(name="click", capability="desktop.click", metadata={"x": 100, "y": 200})
        art = await agent.execute(agent.agent_id, task)
    assert art.content["behavior"] is True
    assert fake_pg.click.called
    types = [e.type for e in await store.read_stream("beh")]
    assert "behavior.mouse_moved" in types
    assert "behavior.mouse_clicked" in types


async def test_scroll_routes_through_behavior() -> None:
    agent, _, store, _ = _agent_with_behavior()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        task = Task(name="scroll", capability="desktop.scroll", metadata={"direction": "down"})
        art = await agent.execute(agent.agent_id, task)
    assert art.type == "desktop.scroll"
    assert fake_pg.scroll.called
    assert any(e.type == "behavior.scrolled" for e in await store.read_stream("beh"))


async def test_type_routes_through_behavior() -> None:
    agent, _, store, _ = _agent_with_behavior()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        task = Task(name="type", capability="desktop.type", metadata={"text": "hi"})
        await agent.execute(agent.agent_id, task)
    assert fake_pg.write.called
    assert any(e.type == "behavior.text_typed" for e in await store.read_stream("beh"))


async def test_read_routes_through_behavior() -> None:
    agent, _, store, _ = _agent_with_behavior()
    await agent.start()
    task = Task(
        name="read",
        capability="desktop.read",
        metadata={"text": "read me now", "region": [0, 0, 300, 50]},
    )
    art = await agent.execute(agent.agent_id, task)
    assert art.type == "desktop.read"
    assert any(e.type == "behavior.reading_progress" for e in await store.read_stream("beh"))


# --- Backward compat ------------------------------------------------------ #
async def test_legacy_click_without_behavior() -> None:
    agent, store = _agent_legacy()
    await agent.start()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.desktop_agent._require_pyautogui", return_value=fake_pg):
        task = Task(name="click", capability="desktop.click", metadata={"x": 1, "y": 2})
        art = await agent.execute(agent.agent_id, task)
    # legacy path → CommandBus → desktop.clicked event, no "behavior" flag
    assert "behavior" not in art.content
    assert any(e.type == "desktop.clicked" for e in await store.read_stream(agent.agent_id))


# --- Vision element center for behavior ----------------------------------- #
async def test_vision_find_element_for_behavior_center() -> None:
    vision = DesktopVision()
    el = UIElement(label="Submit", bbox=(10, 20, 100, 40), confidence=0.9)
    assert el.center == (60, 40)  # 10+50, 20+20
    with patch.object(vision, "find_element", return_value=el):
        found = await vision.find_element_for_behavior("Submit", b"fake")
    assert found is not None
    assert found.center_x == 60 and found.center_y == 40


async def test_vision_find_element_for_behavior_none() -> None:
    vision = DesktopVision()
    with patch.object(vision, "find_element", return_value=None):
        assert await vision.find_element_for_behavior("nope", b"fake") is None
