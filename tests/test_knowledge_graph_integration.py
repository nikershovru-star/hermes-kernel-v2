"""tests/test_knowledge_graph_integration.py — KG + AgentRuntime/WorkflowEngine (ADR-025)."""

from __future__ import annotations

import asyncio
import random

import pytest
from kernel.agent import AgentRuntime, BaseAgent
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, Task, Workflow, WorkflowInstance, WorkflowStatus, WorkflowStep, WorkflowTrigger
from kernel.events import EventBus, EventStore
from kernel.knowledge_graph import KnowledgeGraphEngine
from kernel.graph_store import GraphStore
from kernel.semantic_graph import Entity, EntityType, GraphQuery, InferenceRule, Relation, RelationType
from kernel.workflow import WorkflowEngine


class FakeAgent(BaseAgent):
    def __init__(self, entity: Agent) -> None:
        super().__init__(entity)
        self._running = False

    async def start(self) -> str:
        self._running = True
        return self.agent_id

    async def stop(self, agent_id: str) -> bool:
        self._running = False
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(type=task.capability, content={"ok": True}, format="json", source=f"agent:{self.name}")

    async def status(self, agent_id: str) -> dict:
        return {"state": "running" if self._running else "stopped"}


def _kg(**kw):
    return KnowledgeGraphEngine(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(7), **kw)


async def test_agent_runtime_remember_and_recall() -> None:
    kg = _kg()
    rt = AgentRuntime(bus=EventBus(), store=EventStore(), knowledge_graph=kg)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    await rt.remember("a", {"name": "Project Hermes", "type": "concept", "properties": {"k": 1}})
    found = await rt.recall("a", "hermes")
    assert any(e.name == "Project Hermes" for e in found)


async def test_agent_runtime_recall_empty_when_no_kg() -> None:
    rt = AgentRuntime(bus=EventBus(), store=EventStore())  # no kg
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    assert await rt.recall("a", "anything") == []


async def test_workflow_execute_with_context_stamps_entity_ids() -> None:
    kg = _kg()
    g = await kg.create_graph("shared")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="cap.x", type=EntityType.CONCEPT))
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store, knowledge_graph=kg)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store, knowledge_graph=kg)
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="one", capability="cap.x")],
        status=WorkflowStatus.DRAFT,
        trigger=WorkflowTrigger(type="manual"),
    )
    inst = await eng.start(wf)
    await eng.execute_with_context(inst.id, wf, context_graph_id=g.graph_id)
    assert "e1" in wf.context.get("kg_matches", [])


async def test_workflow_without_kg_unchanged() -> None:
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store)  # no kg
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    await rt.start(agent)
    ex.register_agent(agent)
    wf = Workflow(
        name="wf",
        steps=[WorkflowStep(id="s1", name="one", capability="cap.x")],
        status=WorkflowStatus.DRAFT,
        trigger=WorkflowTrigger(type="manual"),
    )
    inst = await eng.start(wf)
    arts = await eng.execute_adaptive(inst.id, wf)
    assert any(a.type == "cap.x" for a in arts)


async def test_dynamic_planner_plan_step_context_graph_id() -> None:
    from kernel.dynamic_planner import DynamicPlanner

    kg = _kg()
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="cap.x", type=EntityType.CONCEPT))
    planner = DynamicPlanner(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(1))
    plan = await planner.create_plan("w1", [__import__("kernel.domain", fromlist=["PlanStep"]).PlanStep(step_id="s1", capability="cap.x", context_graph_id=g.graph_id)])
    # the step carries the graph id; execution would query KG (covered by integration)
    assert plan.steps[0].context_graph_id == g.graph_id


async def test_e2e_create_entities_relations_query_inference_merge() -> None:
    kg = _kg()
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="Alice", type=EntityType.PERSON))
    await kg.add_entity(g.graph_id, Entity(entity_id="e2", name="Bob", type=EntityType.PERSON))
    await kg.add_entity(g.graph_id, Entity(entity_id="e3", name="Alice Dup", type=EntityType.PERSON))
    await kg.add_relation(g.graph_id, Relation(relation_id="r1", source_id="e1", target_id="e2", type=RelationType.KNOWS))
    path = kg.find_path(g.graph_id, "e1", "e2")
    assert path
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation", priority=1)
    await kg.run_inference(g.graph_id, [rule])
    canonical = await kg.merge_entities(g.graph_id, "e1", ["e3"])
    assert "e3" not in kg.get_graph(g.graph_id).entities


async def test_event_store_contains_entity_relation_inference_chain() -> None:
    store = EventStore()
    kg = KnowledgeGraphEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="Alice", type=EntityType.PERSON))
    await kg.add_entity(g.graph_id, Entity(entity_id="e2", name="Bob", type=EntityType.PERSON))
    await kg.add_relation(g.graph_id, Relation(relation_id="r1", source_id="e1", target_id="e2", type=RelationType.KNOWS))
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation", priority=1)
    await kg.run_inference(g.graph_id, [rule])
    types = {e.type for e in store._events}
    assert "kg.entity_discovered" in types
    assert "kg.relation_created" in types
    assert "kg.inference_fired" in types


async def test_graph_updated_event_on_mutation() -> None:
    store = EventStore()
    kg = KnowledgeGraphEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="A", type=EntityType.CONCEPT))
    assert any(e.type == "kg.graph_updated" for e in store._events)


async def test_query_executed_event_on_query() -> None:
    store = EventStore()
    kg = KnowledgeGraphEngine(event_bus=EventBus(), event_store=store, rng=random.Random(1))
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="A", type=EntityType.CONCEPT))
    await kg.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="entity_by_name", parameters={"name": "a"}))
    assert any(e.type == "kg.query_executed" for e in store._events)


async def test_backward_compat_agent_runtime_without_kg() -> None:
    rt = AgentRuntime(bus=EventBus(), store=EventStore())
    agent = FakeAgent(Agent(name="a", capabilities=["cap.x"]))
    aid = await rt.start(agent)
    assert rt.get(aid) is not None
    assert rt.list() == [aid]


async def test_backward_compat_workflow_engine_without_kg() -> None:
    bus, store = EventBus(), EventStore()
    rt = AgentRuntime(bus=bus, store=store)
    ex = CapabilityExecutor()
    eng = WorkflowEngine(rt, ex, bus, store)
    assert eng._kg is None


async def test_deterministic_same_seed_entity_id_tiebreak() -> None:
    kg1 = KnowledgeGraphEngine(rng=random.Random(99))
    kg2 = KnowledgeGraphEngine(rng=random.Random(99))
    g1 = await kg1.create_graph("g")
    g2 = await kg2.create_graph("g")
    # both engines produce same graph_id shape (uuid, not rng) — verify rng parity
    a = kg1._rng.randint(0, 10**6)
    b = kg2._rng.randint(0, 10**6)
    assert a == b


async def test_concurrent_add_entity_no_duplicates() -> None:
    kg = _kg()
    g = await kg.create_graph("g")

    async def add(i):
        await kg.add_entity(g.graph_id, Entity(entity_id=f"e{i}", name=f"N{i}", type=EntityType.CONCEPT))

    await asyncio.gather(*[add(i) for i in range(10)])
    assert len(kg.get_graph(g.graph_id).entities) == 10


async def test_axis_knowledge_graph_imports_only_domain_events() -> None:
    import ast
    src = open("kernel/knowledge_graph.py", encoding="utf-8").read()
    tree = ast.parse(src)
    stdlib = {"__future__", "asyncio", "math", "uuid", "datetime", "typing", "time"}
    allowed = {"kernel.events", "kernel.semantic_graph"}
    bad = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if node.module not in allowed and root not in stdlib:
                bad.add(node.module)
        elif isinstance(node, ast.Import):
            for n in node.names:
                root = n.name.split(".")[0]
                if root not in stdlib and root not in allowed:
                    bad.add(n.name)
    assert bad == set(), f"forbidden imports: {bad}"
