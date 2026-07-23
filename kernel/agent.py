"""kernel/agent.py — unified Agent runtime contract (ADR-016).

Unifies the formerly split "plugin" (load/unload/get_capabilities) and "agent"
(no common interface) APIs. ``BaseAgent`` is the async lifecycle contract every
runtime agent implements; ``AgentRuntime`` is the registry of *active* agent
instances — the runtime counterpart to ``AgentRegistry`` (which stores the
declarative ``Agent`` metadata entity registered by ``@sdk.agent``).

AXIS CONTRACT: depends on kernel.domain only. Never imports plugins.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from kernel.domain import Agent, Artifact, Task

logger = logging.getLogger("hermes.kernel.agent")


class BaseAgent(ABC):
    """Unified async lifecycle contract for a runnable agent (ADR-016).

    Mirrors ``BasePlugin`` (load/unload/get_capabilities) but async, because an
    agent *executes* tasks and *returns* Artifacts, whereas a plugin *provides*
    capabilities. ``start`` returns the ``Agent`` entity id so the runtime and
    the declarative registry share one identity key.
    """

    def __init__(self, agent_entity: Agent) -> None:
        self._entity = agent_entity

    # -- contract --------------------------------------------------------- #
    @abstractmethod
    async def start(self) -> str:
        """Bring the agent online. Return its agent_id (the Agent entity id)."""

    @abstractmethod
    async def stop(self, agent_id: str) -> bool:
        """Tear the agent down. Return True if it was running."""

    @abstractmethod
    async def execute(self, agent_id: str, task: Task) -> Artifact:
        """Run ``task`` and return a unified ``Artifact`` result."""

    @abstractmethod
    async def status(self, agent_id: str) -> dict[str, Any]:
        """Return live runtime status (state, last task, metrics, ...)."""

    # -- introspection ---------------------------------------------------- #
    @property
    def entity(self) -> Agent:
        return self._entity

    @property
    def agent_id(self) -> str:
        return self._entity.id

    @property
    def name(self) -> str:
        return self._entity.name

    @property
    def capabilities(self) -> list[str]:
        return list(self._entity.capabilities)


class AgentRuntime:
    """Registry + lifecycle driver for *active* ``BaseAgent`` instances.

    This is deliberately separate from ``AgentRegistry`` (which holds the
    declarative ``Agent`` metadata entity). Here we track live, running agent
    objects so the kernel can start/stop/execute them uniformly — the same way
    ``PluginRegistry`` tracks live plugin instances.
    """

    def __init__(self) -> None:
        self._agents: dict[str, BaseAgent] = {}

    async def start(self, agent: BaseAgent) -> str:
        """Start ``agent`` and register it as running. Return its agent_id."""
        agent_id = await agent.start()
        self._agents[agent_id] = agent
        logger.info("AgentRuntime: started %s (%s)", agent.name, agent_id)
        return agent_id

    async def stop(self, agent_id: str) -> bool:
        """Stop a running agent and drop it from the registry."""
        agent = self._agents.get(agent_id)
        if agent is None:
            logger.warning("AgentRuntime.stop: unknown agent %s", agent_id)
            return False
        ok = await agent.stop(agent_id)
        self._agents.pop(agent_id, None)
        return ok

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        """Execute ``task`` on the running agent identified by ``agent_id``."""
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"agent '{agent_id}' is not running")
        return await agent.execute(agent_id, task)

    async def status(self, agent_id: str) -> dict[str, Any]:
        """Return runtime status for a running agent."""
        agent = self._agents.get(agent_id)
        if agent is None:
            return {"agent_id": agent_id, "state": "offline"}
        return await agent.status(agent_id)

    def get(self, agent_id: str) -> BaseAgent | None:
        """Return the live agent instance, or None if not running."""
        return self._agents.get(agent_id)

    def list(self) -> list[str]:
        """List agent_ids of all currently running agents."""
        return list(self._agents.keys())


__all__ = ["BaseAgent", "AgentRuntime"]
