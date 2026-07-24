"""tests/test_cluster.py — ClusterManager (ADR-026).

Deterministic: injectable clock + transport; no real network / asyncio.sleep.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from kernel.cluster import ClusterManager
from kernel.events import EventBus, EventStore
from kernel.marketplace_domain import NodeInfo


def _cm(**kw):
    return ClusterManager(cluster_id="c1", event_bus=EventBus(), event_store=EventStore(), **kw)


async def test_join_and_leave() -> None:
    cm = _cm()
    await cm.join_cluster("n1", "a1", ["c1"])
    assert "n1" in cm.get_topology().nodes
    assert await cm.leave_cluster("n1") is True
    assert "n1" not in cm.get_topology().nodes


async def test_topology_lists_nodes() -> None:
    cm = _cm()
    await cm.join_cluster("n1", "a1", ["c1"])
    await cm.join_cluster("n2", "a2", ["c2"])
    topo = cm.get_topology()
    assert set(topo.nodes.keys()) == {"n1", "n2"}


async def test_leader_election_oldest_node() -> None:
    from datetime import datetime, timezone

    cm = _cm()
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # join n2 first with earlier heartbeat, n1 later
    cm._nodes["n2"] = NodeInfo(node_id="n2", address="a2", last_heartbeat=t0)
    cm._nodes["n1"] = NodeInfo(node_id="n1", address="a1", last_heartbeat=t0 + timedelta(seconds=10))
    leader = cm.elect_leader()
    assert leader == "n2"


async def test_leader_re_elected_after_leave() -> None:
    cm = _cm()
    await cm.join_cluster("n1", "a1", ["c1"])
    await cm.join_cluster("n2", "a2", ["c2"])
    first = cm.get_topology().leader_id
    await cm.leave_cluster(first)
    remaining = cm.get_topology().leader_id
    assert remaining is not None
    assert remaining != first


async def test_broadcast_uses_injected_transport() -> None:
    sent: list[tuple[str, object]] = []

    class Transport:
        async def send(self, node_id, message):
            sent.append((node_id, message))

    cm = _cm(transport=Transport())
    await cm.join_cluster("n1", "a1", ["c1"])
    await cm.join_cluster("n2", "a2", ["c2"])
    delivered = await cm.broadcast({"cmd": "ping"})
    assert delivered == ["n1", "n2"]
    assert len(sent) == 2


async def test_node_timeout_prunes_stale() -> None:
    from datetime import datetime, timezone

    cm = _cm(node_timeout=30.0)
    now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    cm._clock = lambda: now  # type: ignore[assignment]
    await cm.join_cluster("n1", "a1", ["c1"])
    cm._clock = lambda: now + timedelta(seconds=60)  # type: ignore[assignment]
    dropped = cm.prune_timed_out()
    assert "n1" in dropped
    assert "n1" not in cm.get_topology().nodes


async def test_node_joined_event_emitted() -> None:
    store = EventStore()
    cm = ClusterManager(cluster_id="c1", event_bus=EventBus(), event_store=store)
    await cm.join_cluster("n1", "a1", ["c1"])
    assert any(e.type == "mp.node_joined" for e in store._events)


async def test_node_left_event_emitted() -> None:
    store = EventStore()
    cm = ClusterManager(cluster_id="c1", event_bus=EventBus(), event_store=store)
    await cm.join_cluster("n1", "a1", ["c1"])
    await cm.leave_cluster("n1", reason="shutdown")
    assert any(e.type == "mp.node_left" for e in store._events)
