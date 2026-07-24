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

from kernel.domain import Agent, Artifact, HealthCheck, SandboxPolicy, Task
from kernel.events import DomainEvent, EventBus, EventStore
from kernel.health import HealthMonitor
from kernel.sandbox import Sandbox

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

    def __init__(
        self,
        bus: EventBus | None = None,
        store: EventStore | None = None,
        sandbox: Sandbox | None = None,
        health_monitor: HealthMonitor | None = None,
        swarm_coordinator: "SwarmCoordinator | None" = None,
        knowledge_graph: "KnowledgeGraphEngine | None" = None,
    ) -> None:
        self._agents: dict[str, BaseAgent] = {}
        self._bus = bus
        self._store = store
        self._sandbox = sandbox
        self._health = health_monitor
        self._swarm = swarm_coordinator
        self._kg = knowledge_graph
        self._swarm_ids: dict[str, str] = {}  # agent_id -> swarm_id
        self._default_graphs: dict[str, str] = {}  # agent_id -> graph_id

    async def start(self, agent: BaseAgent) -> str:
        """Start ``agent`` and register it as running. Return its agent_id."""
        agent_id = await agent.start()
        self._agents[agent_id] = agent
        logger.info("AgentRuntime: started %s (%s)", agent.name, agent_id)
        if self._health is not None:
            self._health.register(
                component_id=agent_id,
                component_type="agent",
                probe=lambda aid=agent_id: self._probe_agent(aid),
                check=HealthCheck(interval_seconds=10.0),
            )
        await self._publish(
            DomainEvent(
                type="agent.started",
                aggregate_id=agent_id,
                payload={"agent_type": agent.__class__.__name__},
            )
        )
        return agent_id

    async def _probe_agent(self, agent_id: str) -> bool:
        """Liveness probe: agent is healthy iff its status reports 'running'."""
        try:
            status = await self.status(agent_id)
            return status.get("state") == "running"
        except Exception:  # noqa: BLE001
            return False

    async def stop(self, agent_id: str) -> bool:
        """Stop a running agent and drop it from the registry."""
        agent = self._agents.get(agent_id)
        if agent is None:
            logger.warning("AgentRuntime.stop: unknown agent %s", agent_id)
            return False
        ok = await agent.stop(agent_id)
        self._agents.pop(agent_id, None)
        if self._health is not None:
            self._health.unregister(agent_id)
        await self._publish(
            DomainEvent(
                type="agent.stopped",
                aggregate_id=agent_id,
                payload={"reason": "explicit_stop"},
            )
        )
        return ok

    async def execute(
        self,
        agent_id: str,
        task: Task,
        workflow_id: str | None = None,
        policy: SandboxPolicy | None = None,
    ) -> Artifact:
        """Execute ``task`` on the running agent identified by ``agent_id``.

        If ``workflow_id`` is supplied, it is propagated onto ``task.workflow_id``
        (ADR-019: activates the previously-dead ``Task.workflow_id`` field, linking
        the task to its WorkflowInstance).
        """
        if workflow_id is not None:
            task.workflow_id = workflow_id
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"agent '{agent_id}' is not running")
        # Swarm delegation: if the local agent lacks this capability and a
        # coordinator is wired, hand the task to an eligible swarm member.
        # Falls back to local execution otherwise (backward compatible).
        if (
            self._swarm is not None
            and task.capability is not None
            and task.capability not in getattr(agent, "capabilities", [])
        ):
            swarm_id = self._swarm_ids.get(agent_id)
            if swarm_id is not None:
                try:
                    delegation = self._swarm.delegate_task(swarm_id, task, agent_id)
                    member = self._swarm.get_swarm(swarm_id)
                    target = member.members.get(delegation.to_agent) if member else None
                    if target is not None:
                        self._swarm.complete_delegation(
                            delegation.delegation_id,
                            result_summary=f"delegated from {agent_id}",
                        )
                        return Artifact(
                            type="task",
                            content={"delegated_to": delegation.to_agent, "task_id": task.id},
                            format="json",
                        )
                except (KeyError, ValueError):
                    # no eligible member → fall through to local execution
                    pass
        if self._sandbox is None:
            return await agent.execute(agent_id, task)
        # Sandboxed execution: breach cancels + stops the agent (cleanup hook).
        policy = policy or self._default_policy(agent)
        return await self._sandbox.run(
            agent.execute(agent_id, task),
            policy=policy,
            cleanup=lambda: agent.stop(agent_id),
            context={"agent_id": agent_id, "task_id": task.id},
        )

    @staticmethod
    def _default_policy(agent: BaseAgent) -> SandboxPolicy:
        """Permissive default policy (ADR-020): no network/subprocess limits."""
        return SandboxPolicy()

    # -- swarm integration (ADR-023) -- #
    async def join_swarm(self, agent_id: str, swarm_id: str, role: str = "worker") -> None:
        """Register this running agent with a swarm via the coordinator (optional)."""
        if self._swarm is None:
            raise RuntimeError("no swarm_coordinator configured")
        agent = self._agents.get(agent_id)
        if agent is None:
            raise KeyError(f"agent '{agent_id}' is not running")
        caps = list(getattr(agent, "capabilities", []))
        node_id = f"node-{agent_id}"
        await self._swarm.join_swarm(swarm_id, agent_id, node_id, role=role, capabilities=caps)
        self._swarm_ids[agent_id] = swarm_id

    async def leave_swarm(self, agent_id: str, reason: str = "graceful") -> None:
        """Leave the swarm this agent belongs to (optional coordinator)."""
        if self._swarm is None:
            raise RuntimeError("no swarm_coordinator configured")
        swarm_id = self._swarm_ids.get(agent_id)
        if swarm_id is None:
            return
        await self._swarm.leave_swarm(swarm_id, agent_id, reason=reason)
        self._swarm_ids.pop(agent_id, None)

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

    # -- semantic memory (ADR-025) -------------------------------------- #
    async def remember(self, agent_id: str, fact: dict) -> "Entity":
        """Store a fact as an Entity (+ optional Relation) in the agent's graph.

        ``fact`` may contain: ``name``, ``type`` (EntityType value), ``properties``,
        ``source``, ``confidence``, and an optional ``relation`` dict
        (``{target: str, type: str}``) linking to another named entity.
        Returns the stored/merged Entity.
        """
        if self._kg is None:
            raise RuntimeError("AgentRuntime has no knowledge_graph wired")
        import uuid

        from kernel.semantic_graph import Entity, EntityType, Relation, RelationType

        graph_id = self._default_graphs.get(agent_id)
        if graph_id is None:
            g = await self._kg.create_graph(f"agent:{agent_id}")
            graph_id = g.graph_id
            self._default_graphs[agent_id] = graph_id
        ent = Entity(
            entity_id=uuid.uuid4().hex,
            name=fact["name"],
            type=EntityType(fact.get("type", "custom")),
            properties=fact.get("properties", {}),
            source=fact.get("source", "agent"),
            confidence=float(fact.get("confidence", 1.0)),
        )
        stored = await self._kg.add_entity(graph_id, ent)
        rel = fact.get("relation")
        if rel:
            target_name = rel.get("target")
            target_type = rel.get("type")
            target_ent = None
            g = self._kg.get_graph(graph_id)
            for e in g.entities.values():
                if e.name.lower() == str(target_name).lower():
                    target_ent = e
                    break
            if target_ent is None:
                target_ent = await self._kg.add_entity(
                    graph_id,
                    Entity(entity_id=uuid.uuid4().hex, name=str(target_name), type=EntityType(target_type or "custom")),
                )
            await self._kg.add_relation(
                graph_id,
                Relation(
                    relation_id=uuid.uuid4().hex,
                    source_id=stored.entity_id,
                    target_id=target_ent.entity_id,
                    type=RelationType(rel.get("relation_type", "knows")),
                ),
            )
        return stored

    async def recall(self, agent_id: str, query: str) -> list["Entity"]:
        """Query the agent's graph by entity name (case-insensitive substring)."""
        if self._kg is None:
            return []
        graph_id = self._default_graphs.get(agent_id)
        if graph_id is None:
            return []
        import uuid

        from kernel.semantic_graph import GraphQuery

        res = await self._kg.query(
            graph_id,
            GraphQuery(query_id=uuid.uuid4().hex, graph_id=graph_id, query_type="entity_by_name", parameters={"name": query}),
        )
        g = self._kg.get_graph(graph_id)
        return [g.entities[eid] for eid in res.entities if eid in g.entities]

    async def _publish(self, event: DomainEvent) -> None:
        """Append to store + publish on bus if either is configured."""
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)
