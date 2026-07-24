"""tests/test_swarm_coordinator.py — SwarmCoordinator (ADR-023).

Deterministic tests: injectable clock + instant sleep + seeded rng. No real
network/sleep; all communication via the in-memory EventBus/EventStore.
"""

from __future__ import annotations

import random
import time

import pytest
from kernel.domain import SwarmTopology, Task
from kernel.events import EventBus, EventStore
from kernel.swarm import SwarmCoordinator


def _coord(bus=None, store=None, suspicion=3000, failure=10000):
    return SwarmCoordinator(
        event_bus=bus or EventBus(),
        event_store=store or EventStore(),
        rng=random.Random(42),
        clock=lambda: time.monotonic(),
        sleep=_instant,
        suspicion_timeout_ms=suspicion,
        failure_timeout_ms=failure,
    )


async def _instant(_s: float) -> None:
    return None


async def test_create_swarm() -> None:
    c = _coord()
    s = c.create_swarm("s1")
    assert s.swarm_id == "s1"
    assert c.get_swarm("s1") is not None


async def test_join_emits_event_and_auto_leader() -> None:
    bus = EventBus()
    store = EventStore()
    c = _coord(bus, store)
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    joined = [e for e in await store.read_stream("s1") if e.type == "swarm.agent_joined"]
    assert len(joined) == 1
    # first joiner auto-promoted to leader under LEADER_WORKER
    assert c.get_leader("s1") == "a"


async def test_leave_removes_member_and_emits() -> None:
    bus = EventBus()
    store = EventStore()
    c = _coord(bus, store)
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.leave_swarm("s1", "a", reason="graceful")
    assert c.get_swarm("s1").members == {}
    left = [e for e in await store.read_stream("s1") if e.type == "swarm.agent_left"]
    assert left[0].payload["reason"] == "graceful"


async def test_bully_election_highest_agent_id_wins() -> None:
    c = _coord()
    c.create_swarm("s1")
    await c.join_swarm("s1", "agent-a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "agent-z", "n2", capabilities=["y"])
    await c.join_swarm("s1", "agent-m", "n3", capabilities=["z"])
    # force a fresh election (no leader yet) → highest lexicographic id wins
    c.get_swarm("s1").leader_id = None
    await c._run_election("s1")
    assert c.get_leader("s1") == "agent-z"


async def test_leader_re_election_on_leader_departure() -> None:
    store = EventStore()
    c = _coord(EventBus(), store)
    await c.join_swarm("s1", "agent-a", "n1", capabilities=["x"])  # first → leader
    await c.join_swarm("s1", "agent-z", "n2", capabilities=["y"])
    await c.join_swarm("s1", "agent-m", "n3", capabilities=["z"])
    assert c.get_leader("s1") == "agent-a"
    # leader leaves → election among remaining (highest = agent-z)
    await c.leave_swarm("s1", "agent-a", reason="graceful")
    assert c.get_leader("s1") == "agent-z"
    elected = [e for e in await store.read_stream("s1") if e.type == "swarm.leader_elected"]
    assert len(elected) >= 2


async def test_heartbeat_updates_load_score_and_health() -> None:
    c = _coord()
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.handle_heartbeat("n1", "a", load_score=0.7)
    member = c.get_swarm("s1").members["a"]
    assert member.health == "healthy"
    # load_score is tracked per-agent
    assert c._load["a"] == 0.7


async def test_suspicion_then_unhealthy_lifecycle() -> None:
    store = EventStore()
    c = _coord(EventBus(), store, suspicion=3000, failure=10000)
    # join at t=0 so the recorded heartbeat is at 0
    c._clock = lambda: 0.0
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    # phase 1: advance past suspicion (3s) but not failure (10s) → suspected + missed
    c._clock = lambda: 5.0
    await c.check_partitions()
    assert c.get_swarm("s1").members["a"].health == "suspected"
    # HeartbeatMissed is keyed on node_id
    missed = [e for e in await store.read_stream("n1") if e.type == "swarm.heartbeat_missed"]
    assert len(missed) == 1
    # phase 2: advance past failure (10s) → unhealthy + partition
    c._clock = lambda: 1000.0
    partitioned = await c.check_partitions()
    member = c.get_swarm("s1").members["a"]
    assert member.health == "unhealthy"
    assert partitioned == ["a"]
    part = [e for e in await store.read_stream("n1") if e.type == "swarm.node_partitioned"]
    assert len(part) == 1


async def test_suspicion_before_failure() -> None:
    store = EventStore()
    c = _coord(EventBus(), store, suspicion=3000, failure=10000)
    c._clock = lambda: 0.0  # join at t=0
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    c._clock = lambda: 5.0  # advance 5s → past suspicion (3s) but not failure (10s)
    await c.check_partitions()
    assert c.get_swarm("s1").members["a"].health == "suspected"


async def test_partition_escalates_leader_re_election() -> None:
    store = EventStore()
    c = _coord(EventBus(), store, suspicion=3000, failure=10000)
    c._clock = lambda: 0.0  # baseline: just joined, healthy
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])  # first → leader
    await c.join_swarm("s1", "z", "n2", capabilities=["y"])
    assert c.get_leader("s1") == "a"
    # jump clock far into the future (leader 'a' goes stale) but keep 'z' fresh
    c._clock = lambda: 1000.0
    await c.handle_heartbeat("n2", "z", load_score=0.0)  # z stays healthy
    await c.check_partitions()
    assert c.get_swarm("s1").members["a"].health == "unhealthy"
    # new leader elected among remaining healthy → 'z'
    assert c.get_leader("s1") == "z"
    part = [e for e in await store.read_stream("n1") if e.type == "swarm.node_partitioned"]
    assert part[0].payload["affected_agents"] == ["a"]


async def test_suspected_member_not_elected_leader() -> None:
    c = _coord()
    c._clock = lambda: 0.0
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "b", "n2", capabilities=["y"])
    c.get_swarm("s1").leader_id = None
    c.get_swarm("s1").members["b"].health = "suspected"
    # re-run election manually; only healthy 'a' should win
    await c._run_election("s1")
    assert c.get_leader("s1") == "a"


async def test_delegation_round_robin_balances_load() -> None:
    c = _coord()
    c._clock = lambda: 0.0
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "b", "n2", capabilities=["x"])
    # alternate between the two workers
    seen = set()
    for _ in range(4):
        t = Task(name="t", capability="x")
        d = c.delegate_task("s1", t, from_agent="mgr")
        seen.add(d.to_agent)
    assert seen == {"a", "b"}


async def test_delegation_skips_missing_capability() -> None:
    c = _coord()
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "b", "n2", capabilities=["y"])
    t = Task(name="t", capability="y")
    d = c.delegate_task("s1", t, from_agent="mgr")
    assert d.to_agent == "b"


async def test_delegation_prefers_lowest_load() -> None:
    c = _coord()
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])
    await c.join_swarm("s1", "b", "n2", capabilities=["x"])
    c._load["a"] = 0.9
    c._load["b"] = 0.1
    t = Task(name="t", capability="x")
    d = c.delegate_task("s1", t, from_agent="mgr")
    assert d.to_agent == "b"


async def test_partition_escalates_leader_re_election_follower_fresh() -> None:
    store = EventStore()
    c = _coord(EventBus(), store, suspicion=3000, failure=10000)
    c._clock = lambda: 0.0  # join at t=0 so heartbeat is "now"
    await c.join_swarm("s1", "a", "n1", capabilities=["x"])  # first → leader
    await c.join_swarm("s1", "z", "n2", capabilities=["y"])
    assert c.get_leader("s1") == "a"
    # partition the leader's node (keep 'z' fresh so only 'a' goes stale)
    c._clock = lambda: 1000.0
    await c.handle_heartbeat("n2", "z", load_score=0.0)
    await c.check_partitions()
    assert c.get_swarm("s1").members["a"].health == "unhealthy"
    # new leader elected among remaining healthy → 'z'
    assert c.get_leader("s1") == "z"
    part = [e for e in await store.read_stream("n1") if e.type == "swarm.node_partitioned"]
    assert part[0].payload["affected_agents"] == ["a"]
