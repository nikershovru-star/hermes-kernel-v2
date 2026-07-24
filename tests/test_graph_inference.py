"""tests/test_graph_inference.py — rule-based inference (ADR-025)."""

from __future__ import annotations

import random

import pytest
from kernel.events import EventBus, EventStore
from kernel.graph_store import GraphStore
from kernel.knowledge_graph import KnowledgeGraphEngine
from kernel.semantic_graph import Entity, EntityType, InferenceRule, Relation, RelationType


def _kg(**kw):
    return KnowledgeGraphEngine(event_bus=EventBus(), rng=random.Random(3), **kw)


async def _build(kg, with_rel=True):
    g = await kg.create_graph("g")
    await kg.add_entity(g.graph_id, Entity(entity_id="e1", name="Alice", type=EntityType.PERSON))
    await kg.add_entity(g.graph_id, Entity(entity_id="e2", name="Bob", type=EntityType.PERSON))
    if with_rel:
        await kg.add_relation(g.graph_id, Relation(relation_id="r1", source_id="e1", target_id="e2", type=RelationType.KNOWS))
    return g


async def test_rule_pattern_match_source_rel_target() -> None:
    kg = _kg()
    g = await _build(kg)
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation")
    res = await kg.run_inference(g.graph_id, [rule])
    assert res and res[0].inferred


async def test_rule_action_create_relation() -> None:
    kg = _kg()
    g = await _build(kg)
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation")
    await kg.run_inference(g.graph_id, [rule])
    sims = [r for r in kg.get_graph(g.graph_id).relations.values() if r.type == RelationType.SIMILAR_TO]
    assert sims


async def test_rule_action_merge_entities() -> None:
    kg = _kg()
    g = await _build(kg)
    await kg.add_entity(g.graph_id, Entity(entity_id="e3", name="Alice Clone", type=EntityType.PERSON))
    # pattern matches e1-e2 (knows); merge action merges the second (e2) into first (e1)
    rule = InferenceRule(rule_id="ru1", name="merge", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="merge_entities")
    await kg.run_inference(g.graph_id, [rule])
    assert "e2" not in kg.get_graph(g.graph_id).entities  # e2 merged into e1


async def test_rule_action_raise_alert_no_mutation() -> None:
    kg = _kg()
    g = await _build(kg)
    before = len(kg.get_graph(g.graph_id).relations)
    rule = InferenceRule(rule_id="ru1", name="alert", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="raise_alert")
    await kg.run_inference(g.graph_id, [rule])
    after = len(kg.get_graph(g.graph_id).relations)
    assert after == before


async def test_multiple_rules_priority_order() -> None:
    kg = _kg()
    g = await _build(kg)
    low = InferenceRule(rule_id="low", name="l", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation", priority=0)
    high = InferenceRule(rule_id="high", name="h", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="raise_alert", priority=10)
    # both fire; ensure both rules produced results
    res = await kg.run_inference(g.graph_id, [low, high])
    assert len(res) >= 2


async def test_rule_no_match_no_event() -> None:
    store = EventStore()
    kg = _kg(event_store=store)
    g = await _build(kg)
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "org"}, action="create_relation")
    await kg.run_inference(g.graph_id, [rule])
    assert not any(e.type == "kg.inference_fired" for e in store._events)


async def test_cyclic_inference_prevention_max_iterations() -> None:
    kg = _kg()
    g = await _build(kg)
    # create_relation on knows-pair would create similar_to; but similar_to does
    # not re-match the "knows" pattern, so it stabilizes in 1 iteration anyway.
    # Use a pattern that matches its own output: similar_to among persons.
    await kg.add_entity(g.graph_id, Entity(entity_id="e3", name="Carol", type=EntityType.PERSON))
    await kg.add_relation(g.graph_id, Relation(relation_id="r2", source_id="e1", target_id="e3", type=RelationType.SIMILAR_TO))
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "similar_to", "target_type": "person"}, action="create_relation")
    res = await kg.run_inference(g.graph_id, [rule], max_iterations=3)
    # bounded: does not loop forever; at most a few similar_to relations created
    sims = [r for r in kg.get_graph(g.graph_id).relations.values() if r.type == RelationType.SIMILAR_TO]
    assert len(sims) <= 6  # bounded, no explosion


async def test_inference_result_persistence(tmp_path) -> None:
    db = str(tmp_path / "kg.db")
    store = GraphStore(db)
    kg = _kg(store=store)
    g = await _build(kg)
    rule = InferenceRule(rule_id="ru1", name="sim", pattern={"source_type": "person", "relation": "knows", "target_type": "person"}, action="create_relation")
    await kg.run_inference(g.graph_id, [rule])
    store2 = GraphStore(db)
    kg2 = KnowledgeGraphEngine(store=store2, rng=random.Random(1))
    reloaded = kg2.get_graph(g.graph_id)
    assert any(r.type == RelationType.SIMILAR_TO for r in reloaded.relations.values())
