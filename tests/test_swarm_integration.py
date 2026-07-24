"""tests/test_swarm_integration.py — Swarm integration with kernel runtime (ADR-023).

Covers AgentRuntime.join_swarm + delegated execute, WorkflowEngine swarm-aware
scheduling, CapabilityExecutor.discover_remote, and single-agent backward
compatibility (no coordinator → unchanged behavior).
"""

from __future__ import annotations

import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task
from kernel.events import EventBus, EventStore
from kernel.swarm import SwarmCoordinator
from kernel.workflow import WorkflowEngine


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)
        self._running = False
        self.calls: list[str] = []

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        self.calls.append(task.capability)
        return Artifact(type=task.capability, content={"ok": True}, format="json", source=f"agent:{self.name}")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running" if self._running else "stopped"}


def _coord():
    return SwarmCoordinator(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(3))


# --- AgentRuntime joins swarm + delegates ------------------------------- #
async def test_agent_runtime_joins_swarm_and_delegates() -> None:
    bus, store = EventBus(), EventStore()
    coord = _coord()
    rt = AgentRuntime(bus=bus, store=store, swarm_coordinator=coord)
    local = FakeAgent(Agent(name="local", capabilities=["local.cap"]))
    aid = await rt.start(local)
    await rt.join_swarm(aid, "swarm1", role="worker")
    # a remote member with the needed capability
    await coord.join_swarm("swarm1", "remote", "n2", capabilities=["remote.cap"])
    task = Task(name="t", capability="remote.cap")
    art = await rt.execute(aid, task)
    assert art.content.get("delegated_to") == "remote"


async def test_agent_runtime_falls_back_to_local_when_no_member() -> None:
    bus, store = EventBus(), EventStore()
    coord = _coord()
    rt = AgentRuntime(bus=bus, store=store, swarm_coordinator=coord)
    local = FakeAgent(Agent(name="local", capabilities=["local.cap"]))
    aid = await rt.start(local)
    await rt.join_swarm(aid, "swarm1", role="worker")
    # no remote member has the capability → local execution
    task = Task(name="t", capability="local.cap")
    art = await rt.execute(aid, task)
    assert art.type == "local.cap"
    assert local.calls == ["local.cap"]


# --- WorkflowEngine swarm-aware scheduling ------------------------------ #
async def test_workflow_engine_schedule_swarm() -> None:
    bus, store = EventBus(), EventStore()
    coord = _coord()
    runtime = AgentRuntime(bus=bus, store=store, swarm_coordinator=coord)
    ex = CapabilityExecutor()
    engine = WorkflowEngine(runtime, ex, bus, store, swarm_coordinator=coord)
    await coord.join_swarm("s1", "a", "n1", capabilities=["cap.x"])
    await coord.join_swarm("s1", "b", "n2", capabilities=["cap.x"])
    tasks = [Task(name="1", capability="cap.x"), Task(name="2", capability="cap.x")]
    delegations = engine.schedule_swarm("s1", tasks, from_agent="mgr")
    assert len(delegations) == 2
    assert {d.to_agent for d in delegations} <= {"a", "b"}


# --- CapabilityExecutor remote discovery -------------------------------- #
async def test_capability_executor_discover_remote() -> None:
    coord = _coord()
    await coord.join_swarm("s1", "a", "n1", capabilities=["cap.x", "cap.y"])
    await coord.join_swarm("s1", "b", "n2", capabilities=["cap.z"])
    ex = CapabilityExecutor()
    caps = ex.discover_remote("s1", coordinator=coord)
    assert caps == ["cap.x", "cap.y", "cap.z"]


async def test_capability_executor_discover_remote_empty_without_coordinator() -> None:
    ex = CapabilityExecutor()
    assert ex.discover_remote("s1") == []


async def test_capability_executor_discover_remote_skips_unhealthy() -> None:
    coord = _coord()
    await coord.join_swarm("s1", "a", "n1", capabilities=["cap.x"])
    await coord.join_swarm("s1", "b", "n2", capabilities=["cap.y"])
    coord.get_swarm("s1").members["b"].health = "unhealthy"
    ex = CapabilityExecutor()
    caps = ex.discover_remote("s1", coordinator=coord)
    assert caps == ["cap.x"]


# --- Backward compatibility --------------------------------------------- #
async def test_single_agent_without_coordinator_unchanged() -> None:
    rt = AgentRuntime()  # no swarm_coordinator
    local = FakeAgent(Agent(name="local", capabilities=["local.cap"]))
    aid = await rt.start(local)
    task = Task(name="t", capability="local.cap")
    art = await rt.execute(aid, task)
    assert art.type == "local.cap"
    assert local.calls == ["local.cap"]


async def test_workflow_engine_without_coordinator_unchanged() -> None:
    bus, store = EventBus(), EventStore()
    runtime = AgentRuntime(bus=bus, store=store)  # no coordinator
    agent = FakeAgent(Agent(name="local", capabilities=["local.cap"]))
    await runtime.start(agent)
    ex = CapabilityExecutor()
    ex.register_agent(agent)
    engine = WorkflowEngine(runtime, ex, bus, store)  # no coordinator
    assert engine._swarm is None
    # local execute path still works
    art = await agent.execute(agent.agent_id, Task(name="t", capability="local.cap"))
    assert art.type == "local.cap"
