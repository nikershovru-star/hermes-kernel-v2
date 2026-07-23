"""plugins/builtin/agents/echo_agent.py — reference BaseAgent implementation.

A minimal but complete ``BaseAgent`` (ADR-016) used to exercise the unified
agent lifecycle (start/stop/execute/status) and the ``AgentRuntime`` without
pulling in heavy optional deps. Real agents (browser, desktop) can subclass the
same contract.

AXIS CONTRACT: depends on kernel (agent, domain). Never imported by kernel.
"""

from __future__ import annotations

import logging
from typing import Any

from kernel.agent import BaseAgent
from kernel.domain import Agent, Artifact, Task

logger = logging.getLogger("hermes.agent.echo")


class EchoAgent(BaseAgent):
    """Echoes the task back as an Artifact (reference implementation)."""

    def __init__(self, agent_entity: Agent) -> None:
        super().__init__(agent_entity)
        self._running = False
        self._last_task_id: str | None = None

    async def start(self) -> str:
        self._running = True
        logger.info("EchoAgent %s started", self.agent_id)
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        if not self._running:
            return False
        self._running = False
        logger.info("EchoAgent %s stopped", agent_id)
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        if not self._running:
            raise RuntimeError(f"agent {agent_id} is not running")
        self._last_task_id = task.id
        return Artifact(
            type=task.capability or "text",
            content={"echo": task.name, "task_id": task.id},
            format="json",
            source=f"agent:{self.name}",
            provenance=[f"task:{task.id}"],
        )

    async def status(self, agent_id: str) -> dict[str, Any]:
        return {
            "agent_id": agent_id,
            "name": self.name,
            "state": "running" if self._running else "stopped",
            "last_task_id": self._last_task_id,
            "capabilities": self.capabilities,
        }
