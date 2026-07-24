"""kernel/team_manager.py — TeamManager (ADR-023).

AXIS CONTRACT: depends only on ``kernel.domain`` + ``kernel.events`` +
``kernel.swarm`` + ``kernel.workflow`` + stdlib. Never imports plugins/ or mcp/.

High-level facade over :class:`kernel.swarm.SwarmCoordinator` + the kernel's
:class:`kernel.workflow.WorkflowEngine`: create/disband named teams, assign
roles, and run a batch of tasks distributed across swarm members.
"""

from __future__ import annotations

from typing import Optional

from kernel.domain import Artifact, Swarm, SwarmTopology, Task
from kernel.swarm import SwarmCoordinator
from kernel.workflow import WorkflowEngine


class TeamManager:
    """Named-team orchestration on top of SwarmCoordinator."""

    def __init__(
        self,
        coordinator: SwarmCoordinator,
        workflow_engine: Optional[WorkflowEngine] = None,
        store=None,
        executor: Optional[callable] = None,
    ) -> None:
        self._coord = coordinator
        self._wf = workflow_engine
        self._store = store
        # executor(agent_id, task) -> await Artifact  (injected for testability)
        self._executor = executor
        self._teams: dict[str, Swarm] = {}

    # -- lifecycle -------------------------------------------------------- #
    async def create_team(
        self,
        name: str,
        topology: SwarmTopology,
        agent_ids: list[str],
        node_ids: Optional[list[str]] = None,
    ) -> Swarm:
        swarm = self._coord.create_swarm(name, topology)
        node_ids = node_ids or [f"node-{a}" for a in agent_ids]
        for agent_id, node_id in zip(agent_ids, node_ids):
            await self._coord.join_swarm(swarm.swarm_id, agent_id, node_id, role="worker", capabilities=[])
        self._teams[name] = swarm
        if self._store is not None:
            self._store.put(swarm)
        return swarm

    async def assign_role(self, swarm_id: str, agent_id: str, role: str) -> None:
        swarm = self._coord.get_swarm(swarm_id)
        if swarm is None or agent_id not in swarm.members:
            raise KeyError(f"agent {agent_id!r} not in team {swarm_id!r}")
        if role not in ("leader", "worker", "observer"):
            raise ValueError(f"invalid role {role!r}")
        # changing leader explicitly
        if role == "leader":
            swarm.leader_id = agent_id
        swarm.members[agent_id].role = role
        self._coord._persist_swarm(swarm)

    async def disband_team(self, swarm_id: str) -> None:
        swarm = self._coord.get_swarm(swarm_id)
        if swarm is None:
            return
        for agent_id in list(swarm.members.keys()):
            await self._coord.leave_swarm(swarm_id, agent_id, reason="disband")
        self._teams.pop(swarm_id, None)
        if self._store is not None:
            self._store.delete(swarm_id)

    # -- distributed execution ------------------------------------------- #
    async def execute_distributed(self, workflow_id: str, tasks: list[Task]) -> list[Artifact]:
        """Schedule each task to a swarm member via the coordinator.

        Each task is stamped with ``workflow_id`` for traceability and delegated
        to an eligible swarm member. The optional injected ``executor``
        (``async def (agent_id, task) -> Artifact``) actually runs the work; when
        omitted a lightweight Artifact acknowledging the delegation is returned.
        When no swarm / no eligible member exists the task is reported as failed
        locally (no crash) — the caller may retry/re-schedule.
        """
        results: list[Artifact] = []
        for task in tasks:
            task.workflow_id = workflow_id
            from_agent = task.assigned_to or "team-manager"
            try:
                delegation = self._coord.delegate_task(task.swarm_id or workflow_id, task, from_agent)
            except (KeyError, ValueError):
                results.append(
                    Artifact(type="task", content={"error": "no eligible member", "task_id": task.id}, format="json")
                )
                continue
            if self._executor is not None:
                artifact = await self._executor(delegation.to_agent, task)
            else:
                artifact = Artifact(
                    type="task",
                    content={"delegated_to": delegation.to_agent, "task_id": task.id},
                    format="json",
                )
            self._coord.complete_delegation(delegation.delegation_id, result_summary=f"ran on {delegation.to_agent}")
            results.append(artifact)
        return results

    def get_team(self, swarm_id: str) -> Optional[Swarm]:
        return self._coord.get_swarm(swarm_id)


__all__ = ["TeamManager"]
