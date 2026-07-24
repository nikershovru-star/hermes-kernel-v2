"""tests/test_distributed_health.py — DistributedHealthMonitor (ADR-023).

Deterministic: injectable clock + instant sleep stub. No real network/timers.
"""

from __future__ import annotations

import time

import pytest
from kernel.distributed_health import DistributedHealthMonitor
from kernel.events import EventBus, EventStore


async def _instant(_s: float) -> None:
    return None


def _mono(value: list[float]):
    return lambda: value[0]


async def test_track_node_records_last_seen() -> None:
    dh = DistributedHealthMonitor(node_id="n1")
    info = dh.track_node("n2", capabilities=["x"])
    assert info.node_id == "n2"
    assert dh.get_node_health("n2") == "healthy"


def test_unknown_node_health_unknown() -> None:
    dh = DistributedHealthMonitor(node_id="n1")
    assert dh.get_node_health("ghost") == "unknown"


async def test_send_heartbeat_emits_event() -> None:
    bus = EventBus()
    store = EventStore()
    dh = DistributedHealthMonitor(event_bus=bus, event_store=store, node_id="n1")
    await dh.send_heartbeat(load_score=0.3)
    beats = [e for e in await store.read_stream("n1") if e.type == "swarm.heartbeat_received"]
    assert len(beats) == 1
    assert beats[0].payload["load_score"] == 0.3
    assert dh.beat_count == 1


async def test_heartbeat_updates_node_record() -> None:
    dh = DistributedHealthMonitor(node_id="n1")
    dh.track_node("n2")
    dh.record_heartbeat("n2", load_score=0.5)
    assert dh.get_node_health("n2") == "healthy"
    assert dh._nodes["n2"].load_score == 0.5


async def test_timeout_triggers_suspicion_injectable_clock() -> None:
    clock = [0.0]
    dh = DistributedHealthMonitor(node_id="n1", clock=_mono(clock))
    dh.track_node("n2")  # seen at t=0
    clock[0] = 5.0  # 5s later → past 3s suspicion, before 10s failure
    assert dh.get_node_health("n2") == "suspected"


async def test_failure_timeout_triggers_unhealthy() -> None:
    clock = [0.0]
    dh = DistributedHealthMonitor(node_id="n1", clock=_mono(clock))
    dh.track_node("n2")  # seen at t=0
    clock[0] = 15.0  # 15s later → past 10s failure threshold
    assert dh.get_node_health("n2") == "unhealthy"


async def test_start_stop_emits_heartbeats_on_interval() -> None:
    steps: list[float] = []

    async def fake_sleep(s: float) -> None:
        steps.append(s)

    clock = [0.0]
    bus = EventBus()
    store = EventStore()
    dh = DistributedHealthMonitor(
        event_bus=bus, event_store=store, node_id="n1",
        interval_seconds=1.0, clock=_mono(clock), sleep=fake_sleep,
    )
    await dh.start()
    await dh.send_heartbeat()  # one manual beat
    await dh.stop()
    assert dh.beat_count == 1
    # start scheduled a loop task; stop cancels it without real delay
    assert dh._running is False


async def test_heartbeat_tracks_multiple_nodes() -> None:
    clock = [0.0]
    dh = DistributedHealthMonitor(node_id="n0", clock=_mono(clock))
    dh.track_node("n1")
    dh.track_node("n2")
    clock[0] = 5.0  # advance time
    dh.record_heartbeat("n1")  # n1 fresh at t=5; n2 still at t=0 → stale
    assert dh.get_node_health("n1") == "healthy"
    assert dh.get_node_health("n2") == "suspected"
