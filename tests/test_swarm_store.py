"""tests/test_swarm_store.py — SwarmStore persistence (ADR-023)."""

from __future__ import annotations

import pytest
from kernel.domain import Swarm, TaskDelegation
from kernel.swarm_store import SwarmStore


def test_put_get_delete_memory() -> None:
    s = SwarmStore()
    swarm = Swarm(swarm_id="s1")
    s.put(swarm)
    assert s.get("s1") is not None
    assert s.delete("s1") is True
    assert s.get("s1") is None
    assert s.delete("s1") is False  # already gone


def test_delegation_persistence_memory() -> None:
    s = SwarmStore()
    d = TaskDelegation(delegation_id="d1", task_id="t1", from_agent="a", to_agent="b", swarm_id="s1")
    s.put_delegation(d)
    assert s.get_delegation("d1").to_agent == "b"
    assert s.delegations_for("s1")[0].delegation_id == "d1"


def test_sqlite_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "sw.db")
    s = SwarmStore(db)
    s.put(Swarm(swarm_id="s1"))
    s.put_delegation(TaskDelegation(delegation_id="d1", task_id="t1", from_agent="a", to_agent="b", swarm_id="s1"))
    # new instance reloads both
    s2 = SwarmStore(db)
    assert s2.get("s1") is not None
    assert s2.get_delegation("d1").to_agent == "b"
    # delete persists
    s2.delete("s1")
    s3 = SwarmStore(db)
    assert s3.get("s1") is None


def test_sqlite_delete_cascades_delegations(tmp_path) -> None:
    db = str(tmp_path / "sw.db")
    s = SwarmStore(db)
    s.put(Swarm(swarm_id="s1"))
    s.put_delegation(TaskDelegation(delegation_id="d1", task_id="t1", from_agent="a", to_agent="b", swarm_id="s1"))
    s.delete("s1")
    s2 = SwarmStore(db)
    assert s2.delegations_for("s1") == []
