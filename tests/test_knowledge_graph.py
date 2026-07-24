"""tests/test_knowledge_graph.py — KnowledgeGraphEngine (ADR-025).

Deterministic: injectable rng, in-memory bus/store, mock embedding_fn, no real
asyncio.sleep.
"""

from __future__ import annotations

import asyncio
import random

import pytest
from kernel.domain import Plan, PlanStatus, PlanStep
from kernel.events import EventBus, EventStore
from kernel.knowledge_graph import KnowledgeGraphEngine
from kernel.semantic_graph import (
    Entity,
    EntityType,
    GraphQuery,
    InferenceRule,
    KnowledgeGraph,
    QueryResult,
    Relation,
    RelationType,
)


def _engine(**kw):
    return KnowledgeGraphEngine(event_bus=EventBus(), event_store=EventStore(), rng=random.Random(42), **kw)


def _ent(eid, name, etype=EntityType.PERSON):
    return Entity(entity_id=eid, name=name, type=etype)


def _rel(rid, s, t, rt=RelationType.KNOWS):
    return Relation(relation_id=rid, source_id=s, target_id=t, type=rt)


async def test_create_and_delete_graph() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    assert eng.get_graph(g.graph_id) is not None
    assert await eng.delete_graph(g.graph_id) is True
    assert eng.get_graph(g.graph_id) is None
    assert await eng.delete_graph(g.graph_id) is False


async def test_add_entity_dedup_by_name() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    a = await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    b = await eng.add_entity(g.graph_id, Entity(entity_id="e2", name="alice", type=EntityType.PERSON, properties={"k": 1}))
    # same name (case-insensitive) -> merge, returns original id
    assert b.entity_id == a.entity_id
    assert b.properties == {"k": 1}
    assert len(eng.get_graph(g.graph_id).entities) == 1


async def test_add_relation_validates_entities() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Bob"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    with pytest.raises(ValueError):
        await eng.add_relation(g.graph_id, _rel("r2", "e1", "missing"))


async def test_get_neighbors_unfiltered() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Bob"))
    await eng.add_entity(g.graph_id, _ent("e3", "Carol"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    await eng.add_relation(g.graph_id, _rel("r2", "e1", "e3"))
    nbrs = eng.get_neighbors(g.graph_id, "e1")
    assert {n[0].entity_id for n in nbrs} == {"e2", "e3"}


async def test_get_neighbors_filtered_by_type() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Bob"))
    await eng.add_entity(g.graph_id, _ent("e3", "Work", EntityType.ORG))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    await eng.add_relation(g.graph_id, _rel("r2", "e1", "e3", RelationType.PART_OF))
    nbrs = eng.get_neighbors(g.graph_id, "e1", RelationType.KNOWS)
    assert {n[0].entity_id for n in nbrs} == {"e2"}


async def test_find_path_exists() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    await eng.add_entity(g.graph_id, _ent("e2", "B"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    path = eng.find_path(g.graph_id, "e1", "e2")
    assert path is not None and len(path) == 1


async def test_find_path_not_exists() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    await eng.add_entity(g.graph_id, _ent("e2", "B"))
    assert eng.find_path(g.graph_id, "e1", "e2") is None


async def test_query_entity_by_name_case_insensitive() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Bob"))
    res = await eng.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="entity_by_name", parameters={"name": "ali"}))
    assert res.entities == ["e1"]


async def test_query_neighbors() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    await eng.add_entity(g.graph_id, _ent("e2", "B"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    res = await eng.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="neighbors", parameters={"entity_id": "e1"}))
    assert res.entities == ["e2"]


async def test_query_path() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    await eng.add_entity(g.graph_id, _ent("e2", "B"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    res = await eng.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="path", parameters={"from_entity_id": "e1", "to_entity_id": "e2"}))
    assert res.relations == ["r1"]


async def test_query_similar_with_embedding() -> None:
    vocab = ["machine", "deep", "learning", "cooking"]

    def emb(s):
        toks = s.lower().split()
        return [1.0 if w in toks else 0.0 for w in vocab]

    eng = _engine(embedding_fn=emb)
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("a", "Machine Learning"))
    await eng.add_entity(g.graph_id, _ent("b", "Deep Learning"))
    await eng.add_entity(g.graph_id, _ent("c", "Cooking"))
    res = await eng.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="similar", parameters={"name": "Learning"}, limit=5))
    assert res.entities[0] in ("a", "b")


async def test_query_similar_fallback_jaccard() -> None:
    eng = _engine()  # no embedding_fn -> Jaccard
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("a", "machine learning model"))
    await eng.add_entity(g.graph_id, _ent("b", "cooking recipe"))
    res = await eng.query(g.graph_id, GraphQuery(query_id="q", graph_id=g.graph_id, query_type="similar", parameters={"name": "learning model"}, limit=5))
    assert res.entities[0] == "a"


async def test_run_inference_creates_relation() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Bob"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e2"))
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation", priority=1)
    res = await eng.run_inference(g.graph_id, [rule])
    assert res[0].inferred  # a similar_to relation created
    sim = [r for r in eng.get_graph(g.graph_id).relations.values() if r.type == RelationType.SIMILAR_TO]
    assert sim


async def test_run_inference_no_match_disabled() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "org"}, action="create_relation", enabled=False)
    res = await eng.run_inference(g.graph_id, [rule])
    # disabled rule is skipped entirely -> no result, no event
    assert res == []
    assert not any(r.type == RelationType.SIMILAR_TO for r in eng.get_graph(g.graph_id).relations.values())


async def test_merge_entities_redirects_relations() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "Alice"))
    await eng.add_entity(g.graph_id, _ent("e2", "Alice Dup"))
    await eng.add_entity(g.graph_id, _ent("e3", "Bob"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e3"))
    await eng.add_relation(g.graph_id, _rel("r2", "e2", "e3"))
    canonical = await eng.merge_entities(g.graph_id, "e1", ["e2"])
    assert "e2" not in eng.get_graph(g.graph_id).entities
    # relations from e2 now point to e1
    assert all(r.source_id != "e2" for r in eng.get_graph(g.graph_id).relations.values())


async def test_graph_version_increments_on_update() -> None:
    eng = _engine()
    g = await eng.create_graph("g")
    v0 = g.version
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    v1 = eng.get_graph(g.graph_id).version
    assert v1 == v0 + 1


async def test_persistence_roundtrip(tmp_path) -> None:
    from kernel.graph_store import GraphStore

    db = str(tmp_path / "kg.db")
    store = GraphStore(db)
    eng = _engine(store=store)
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    store2 = GraphStore(db)
    eng2 = KnowledgeGraphEngine(store=store2, rng=random.Random(1))
    loaded = eng2.get_graph(g.graph_id)
    assert loaded is not None
    assert "e1" in loaded.entities


async def test_list_graphs() -> None:
    eng = _engine()
    await eng.create_graph("g1")
    await eng.create_graph("g2")
    assert len(eng.list_graphs()) == 2


async def test_delete_graph_cascades() -> None:
    from kernel.graph_store import GraphStore

    db = str(tmp_path / "kg.db") if False else ":memory:"
    store = GraphStore()
    eng = _engine(store=store)
    g = await eng.create_graph("g")
    await eng.add_entity(g.graph_id, _ent("e1", "A"))
    await eng.add_relation(g.graph_id, _rel("r1", "e1", "e1"))
    assert await eng.delete_graph(g.graph_id) is True
    assert eng.get_graph(g.graph_id) is None
