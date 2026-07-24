"""kernel/swarm.py — SwarmCoordinator (ADR-023).

AXIS CONTRACT: depends only on ``kernel.domain`` + ``kernel.events`` + stdlib
(asyncio). Never imports plugins/ or mcp/.

Multi-agent orchestration for the v5 Capability Platform: leader election (bully),
heartbeat-based suspicion/failure, task delegation (capability-aware, lowest-load
round-robin), and partition detection. All time/IO is injectable so tests run
deterministically with no real sleep.
"""

from __future__ import annotations

import asyncio
import random
import time
import uuid
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from kernel.domain import (
    Swarm,
    SwarmMember,
    SwarmTopology,
    Task,
    TaskDelegation,
)
from kernel.events import (
    AgentJoinedSwarm,
    AgentLeftSwarm,
    HeartbeatMissed,
    HeartbeatReceived,
    LeaderElected,
    NodePartitioned,
    TaskCompleted,
    TaskDelegated,
)
from kernel.swarm_store import SwarmStore


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SwarmCoordinator:
    """Coordinates a set of swarms: membership, leadership, delegation, health.

    Injectables (all optional):
      - ``clock``: ``() -> float`` monotonic seconds for deterministic timeouts.
      - ``sleep``: ``async (float) -> None`` stub (no real delay in tests).
      - ``rng``: ``random.Random`` for deterministic round-robin tie-breaks.
      - ``event_bus``: fired for swarm.* events.
      - ``event_store``: async EventStore; swarm.* events appended in async paths.
      - ``store``: SwarmStore for swarm/delegation persistence.
      - ``health_monitor``: an ADR-021 HealthMonitor (optional integration).
    """

    def __init__(
        self,
        event_bus: Optional[object] = None,
        event_store: Optional[object] = None,
        health_monitor: Optional[object] = None,
        store: Optional[SwarmStore] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
        rng: Optional[random.Random] = None,
        suspicion_timeout_ms: int = 3000,
        failure_timeout_ms: int = 10000,
    ) -> None:
        self._bus = event_bus
        self._ev_store = event_store
        self._health = health_monitor
        self._store: SwarmStore = store if store is not None else SwarmStore()
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._suspicion_ms = suspicion_timeout_ms
        self._failure_ms = failure_timeout_ms
        self._check_interval = max(suspicion_timeout_ms / 3.0 / 1000.0, 0.001)
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._rr: dict[str, str] = {}  # swarm_id -> last-selected agent_id
        self._delegations: dict[str, TaskDelegation] = {}
        self._nodes: dict[str, dict] = {}
        self._load: dict[str, float] = {}  # agent_id -> latest load_score
        self._last_beat: dict[str, float] = {}  # agent_id -> last heartbeat (monotonic secs)

    # -- event publishing ------------------------------------------------- #
    async def _publish(self, event) -> None:
        """Append (async) then publish on the bus for a domain event."""
        if self._ev_store is not None:
            await self._ev_store.append(event)  # type: ignore[attr-defined]
        if self._bus is not None:
            self._bus.publish(event)  # type: ignore[attr-defined]

    def _publish_sync(self, event) -> None:
        """Publish on the bus only (for synchronous call sites)."""
        if self._bus is not None:
            self._bus.publish(event)  # type: ignore[attr-defined]

    def _persist_swarm(self, swarm: Swarm) -> None:
        self._store.put(swarm)

    # -- lifecycle -------------------------------------------------------- #
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._check_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _check_loop(self) -> None:
        while self._running:
            try:
                await self._sleep(self._check_interval)
            except asyncio.CancelledError:
                break
            await self.check_partitions()

    # -- swarm membership ------------------------------------------------ #
    def create_swarm(self, swarm_id: str, topology: SwarmTopology = SwarmTopology.LEADER_WORKER) -> Swarm:
        if swarm_id in self._store.list():
            return self._store.get(swarm_id)  # type: ignore[union-attr]
        swarm = Swarm(swarm_id=swarm_id, topology=topology)
        self._persist_swarm(swarm)
        return swarm

    async def join_swarm(
        self,
        swarm_id: str,
        agent_id: str,
        node_id: str,
        role: str = "worker",
        capabilities: Optional[list[str]] = None,
    ) -> SwarmMember:
        swarm = self._store.get(swarm_id)
        if swarm is None:
            swarm = self.create_swarm(swarm_id)
        member = SwarmMember(
            agent_id=agent_id,
            node_id=node_id,
            role=role,
            health="healthy",
            capabilities=list(capabilities or []),
        )
        swarm.members[agent_id] = member
        swarm.updated_at = _now()
        self._last_beat[agent_id] = self._clock()
        self._persist_swarm(swarm)
        # always emit join; election is a separate concern
        await self._publish(AgentJoinedSwarm(swarm_id, agent_id, node_id, role, member.capabilities))
        # LEADER_WORKER: auto-promote first joiner if no leader yet.
        if swarm.topology == SwarmTopology.LEADER_WORKER and swarm.leader_id is None:
            await self._run_election(swarm_id)
        return member

    async def leave_swarm(self, swarm_id: str, agent_id: str, reason: str = "graceful") -> None:
        swarm = self._store.get(swarm_id)
        if swarm is None or agent_id not in swarm.members:
            return
        was_leader = swarm.leader_id == agent_id
        del swarm.members[agent_id]
        swarm.updated_at = _now()
        await self._publish(AgentLeftSwarm(swarm_id, agent_id, reason))
        if was_leader:
            swarm.leader_id = None
            await self._run_election(swarm_id)
        else:
            self._persist_swarm(swarm)

    # -- leadership (bully) ---------------------------------------------- #
    async def _run_election(self, swarm_id: str) -> Optional[str]:
        swarm = self._store.get(swarm_id)
        if swarm is None:
            return None
        previous = swarm.leader_id
        candidates = [m for m in swarm.members.values() if m.health == "healthy"]
        new_leader = None
        if candidates:
            new_leader = sorted(candidates, key=lambda m: m.agent_id)[-1].agent_id
        if new_leader != previous:
            swarm.leader_id = new_leader
            swarm.updated_at = _now()
            self._persist_swarm(swarm)
            await self._publish(
                LeaderElected(swarm_id, new_leader, previous, algorithm="bully")
            )
        else:
            self._persist_swarm(swarm)
        return new_leader

    def get_leader(self, swarm_id: str) -> Optional[str]:
        swarm = self._store.get(swarm_id)
        return swarm.leader_id if swarm is not None else None

    # -- heartbeat / health ---------------------------------------------- #
    def track_node(self, node_id: str, capabilities: Optional[list[str]] = None) -> None:
        self._nodes[node_id] = {
            "node_id": node_id,
            "capabilities": list(capabilities or []),
            "load_score": 0.0,
            "last_seen": self._clock(),
            "agent_ids": [],
        }

    async def handle_heartbeat(self, node_id: str, agent_id: str, load_score: float = 0.0) -> None:
        for swarm in self._store.list():
            m = swarm.members.get(agent_id)
            if m is not None and m.node_id == node_id:
                m.last_heartbeat = _now()
                m.health = "healthy"
                swarm.updated_at = _now()
                self._persist_swarm(swarm)
        node = self._nodes.get(node_id)
        if node is not None:
            node["last_seen"] = self._clock()
            node["load_score"] = load_score
            if agent_id not in node["agent_ids"]:
                node["agent_ids"].append(agent_id)
        self._load[agent_id] = load_score
        self._last_beat[agent_id] = self._clock()
        await self._publish(HeartbeatReceived(node_id, agent_id, self._clock(), load_score))

    async def check_partitions(self) -> list[str]:
        now = self._clock() * 1000.0
        partitioned: list[str] = []
        for swarm in list(self._store.list()):
            for agent_id, m in list(swarm.members.items()):
                last_ms = self._last_beat.get(agent_id, 0.0) * 1000.0
                age = now - last_ms
                if age >= self._failure_ms and m.health != "unhealthy":
                    m.health = "unhealthy"
                    affected = [a for a, mm in swarm.members.items() if mm.node_id == m.node_id]
                    await self._publish(NodePartitioned(m.node_id, "failure_timeout", affected))
                    partitioned.append(agent_id)
                    if swarm.leader_id == agent_id:
                        swarm.leader_id = None
                        await self._run_election(swarm.swarm_id)
                    else:
                        self._persist_swarm(swarm)
                elif age >= self._suspicion_ms and m.health == "healthy":
                    m.health = "suspected"
                    missed = int(age // self._suspicion_ms)
                    await self._publish(HeartbeatMissed(m.node_id, agent_id, missed, "suspected"))
                    self._persist_swarm(swarm)
        return partitioned

    # -- delegation ------------------------------------------------------- #
    def delegate_task(self, swarm_id: str, task: Task, from_agent: str) -> TaskDelegation:
        swarm = self._store.get(swarm_id)
        if swarm is None:
            raise KeyError(f"swarm '{swarm_id}' not found")
        cap = task.capability
        eligible = [
            m
            for m in swarm.members.values()
            if m.agent_id != from_agent
            and m.health in ("healthy", "suspected")
            and (cap is None or cap in m.capabilities)
        ]
        if not eligible:
            raise ValueError(f"no eligible member for capability {cap!r} in swarm {swarm_id}")
        eligible.sort(
            key=lambda m: (self._load.get(m.agent_id, 0.0), self._rr.get(swarm_id) == m.agent_id, m.agent_id)
        )
        chosen = eligible[0]
        self._rr[swarm_id] = chosen.agent_id
        delegation_id = uuid.uuid4().hex
        d = TaskDelegation(
            delegation_id=delegation_id,
            task_id=task.id,
            from_agent=from_agent,
            to_agent=chosen.agent_id,
            swarm_id=swarm_id,
            status="pending",
        )
        task.assigned_to = chosen.agent_id
        self._delegations[delegation_id] = d
        self._store.put_delegation(d)
        self._publish_sync(TaskDelegated(delegation_id, task.id, from_agent, chosen.agent_id, swarm_id))
        return d

    def complete_delegation(self, delegation_id: str, result_summary: str = "") -> TaskDelegation:
        d = self._delegations.get(delegation_id)
        if d is None:
            raise KeyError(f"delegation {delegation_id!r} not found")
        d.status = "completed"
        self._store.put_delegation(d)
        self._publish_sync(TaskCompleted(delegation_id, d.task_id, result_summary))
        return d

    def get_swarm(self, swarm_id: str) -> Optional[Swarm]:
        return self._store.get(swarm_id)

    def list_swarms(self) -> list[Swarm]:
        return self._store.list()

    def get_delegation(self, delegation_id: str) -> Optional[TaskDelegation]:
        return self._delegations.get(delegation_id)

    def delegations_for(self, swarm_id: str) -> list[TaskDelegation]:
        return self._store.delegations_for(swarm_id)


__all__ = ["SwarmCoordinator"]
