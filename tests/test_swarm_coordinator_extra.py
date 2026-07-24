"""tests/test_swarm_coordinator_extra.py — additional SwarmCoordinator coverage (ADR-023)."""

from __future__ import annotations

import asyncio
import pytest
from kernel.domain import Task
from kernel.events import EventBus, EventStore
from kernel.swarm import SwarmCoordinator
from tests.test_swarm_coordinator import _coord


async def test_start_stop_background_loop() -> None:
    c = _coord()
    c._check_interval = 0.0
    await c.start()
    assert c._running is True
    await c.stop()
    assert c._running is False


async def test_complete_delegation_emits_event() -> None:
    bus = EventBus()
    store = EventStore()
    received: list = []

    async def _on(e):
        received.append(e)

    bus.subscribe("swarm.task_completed", _on)
    c = _coord(bus, store)
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "b", "n2", capabilities=["x"])
    t = Task(name="t", capability="x")
    d = c.delegate_task("s1", t, from_agent="mgr")
    c.complete_delegation(d.delegation_id, result_summary="done")
    await asyncio.sleep(0)  # let the bus deliver the fire-and-forget event
    assert any(e.type == "swarm.task_completed" for e in received)
    assert c.get_delegation(d.delegation_id).status == "completed"


async def test_track_node_records_capabilities() -> None:
    c = _coord()
    c.track_node("n9", capabilities=["cap.a"])
    assert c._nodes["n9"]["capabilities"] == ["cap.a"]


async def test_delegation_value_error_when_no_member() -> None:
    c = _coord()
    c.create_swarm("s1")
    t = Task(name="t", capability="x")
    with pytest.raises(ValueError):
        c.delegate_task("s1", t, from_agent="mgr")


async def test_delegation_key_error_unknown_swarm() -> None:
    c = _coord()
    t = Task(name="t", capability="x")
    with pytest.raises(KeyError):
        c.delegate_task("ghost", t, from_agent="mgr")


async def test_check_partitions_empty_swarm_noop() -> None:
    c = _coord()
    c.create_swarm("s1")
    assert await c.check_partitions() == []


async def test_handle_heartbeat_updates_node_tracking() -> None:
    c = _coord()
    c.track_node("n1")
    await c.handle_heartbeat("n1", "a", load_score=0.4)
    assert c._nodes["n1"]["load_score"] == 0.4
    assert "a" in c._nodes["n1"]["agent_ids"]
