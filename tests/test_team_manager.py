"""tests/test_team_manager.py — TeamManager (ADR-023).

Verifies team lifecycle, role assignment, distributed execution wiring, and
persistence round-trip. In-memory coordinator + EventBus/EventStore, SQLite via
``:memory:`` for the persistence test.
"""

from __future__ import annotations

import random

import pytest
from kernel.domain import Artifact, SwarmTopology, Task
from kernel.events import EventBus, EventStore
from kernel.swarm import SwarmCoordinator
from kernel.swarm_store import SwarmStore
from kernel.team_manager import TeamManager


def _coord(**kw):
    return SwarmCoordinator(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(7), **kw)


async def _executor(agent_id: str, task: Task) -> Artifact:
    return Artifact(type="task", content={"ran_on": agent_id, "cap": task.capability}, format="json")


async def test_create_team_members_joined() -> None:
    c = _coord()
    tm = TeamManager(c)
    swarm = await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b", "c"])
    assert set(swarm.members.keys()) == {"a", "b", "c"}
    assert tm.get_team("team1") is not None


async def test_create_team_auto_leader() -> None:
    c = _coord()
    tm = TeamManager(c)
    swarm = await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b"])
    assert swarm.leader_id is not None  # first joiner promoted


async def test_assign_role_sets_member_and_leader() -> None:
    c = _coord()
    tm = TeamManager(c)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b"])
    await tm.assign_role("team1", "b", "leader")
    swarm = tm.get_team("team1")
    assert swarm.leader_id == "b"
    assert swarm.members["b"].role == "leader"


async def test_assign_role_rejects_invalid() -> None:
    c = _coord()
    tm = TeamManager(c)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a"])
    with pytest.raises(ValueError):
        await tm.assign_role("team1", "a", "wizard")


async def test_assign_role_unknown_agent_raises() -> None:
    c = _coord()
    tm = TeamManager(c)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a"])
    with pytest.raises(KeyError):
        await tm.assign_role("team1", "ghost", "worker")


async def test_disband_team_removes_members() -> None:
    c = _coord()
    tm = TeamManager(c)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b"])
    await tm.disband_team("team1")
    # swarm object persists but with no members
    assert tm.get_team("team1") is not None
    assert c.get_swarm("team1").members == {}


async def test_execute_distributed_runs_on_members() -> None:
    c = _coord()
    tm = TeamManager(c, executor=_executor)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b"])
    # give members capabilities so delegation can match
    for aid in ("a", "b"):
        c.get_swarm("team1").members[aid].capabilities = ["cap"]
    tasks = [Task(name="1", capability="cap", swarm_id="team1"), Task(name="2", capability="cap", swarm_id="team1")]
    arts = await tm.execute_distributed("wf1", tasks)
    assert len(arts) == 2
    assert {a.content["ran_on"] for a in arts} <= {"a", "b"}


async def test_execute_distributed_persistence_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "swarms.db")
    store = SwarmStore(db)
    c = SwarmCoordinator(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(7), store=store)
    tm = TeamManager(c, executor=_executor, store=store)
    await tm.create_team("team1", SwarmTopology.LEADER_WORKER, ["a", "b"])
    # a fresh store instance (same file) reloads the persisted swarm
    store2 = SwarmStore(db)
    loaded = store2.get("team1")
    assert loaded is not None
    assert set(loaded.members.keys()) == {"a", "b"}
