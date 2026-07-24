"""tests/test_graph_store.py — GraphStore persistence (ADR-025)."""

from __future__ import annotations

import pytest
from kernel.graph_store import GraphStore
from kernel.semantic_graph import Entity, EntityType, KnowledgeGraph, Relation, RelationType


def _g(name="g"):
    return KnowledgeGraph(graph_id="g1", name=name)


def test_put_get_delete_graph_memory() -> None:
    s = GraphStore()
    s.put(_g())
    assert s.get("g1") is not None
    assert s.delete_graph("g1") is True
    assert s.get("g1") is None
    assert s.delete_graph("g1") is False


def test_entity_relation_persistence_memory() -> None:
    s = GraphStore()
    g = _g()
    g.entities["e1"] = Entity(entity_id="e1", name="A", type=EntityType.CONCEPT)
    g.relations["r1"] = Relation(relation_id="r1", source_id="e1", target_id="e1", type=RelationType.KNOWS)
    s.put(g)
    loaded = s.get("g1")
    assert loaded.entities["e1"].name == "A"
    assert loaded.relations["r1"].type == RelationType.KNOWS


def test_sqlite_roundtrip(tmp_path) -> None:
    db = str(tmp_path / "kg.db")
    s = GraphStore(db)
    g = _g()
    g.entities["e1"] = Entity(entity_id="e1", name="A", type=EntityType.CONCEPT)
    g.relations["r1"] = Relation(relation_id="r1", source_id="e1", target_id="e1", type=RelationType.KNOWS)
    s.put(g)
    s2 = GraphStore(db)
    loaded = s2.get("g1")
    assert loaded.entities["e1"].name == "A"
    assert loaded.relations["r1"].type == RelationType.KNOWS


def test_list_graphs_after_reload(tmp_path) -> None:
    db = str(tmp_path / "kg.db")
    s = GraphStore(db)
    s.put(KnowledgeGraph(graph_id="g1", name="a"))
    s.put(KnowledgeGraph(graph_id="g2", name="b"))
    s2 = GraphStore(db)
    assert len(s2.list_graphs()) == 2


def test_delete_graph_cascades_sqlite(tmp_path) -> None:
    db = str(tmp_path / "kg.db")
    s = GraphStore(db)
    g = KnowledgeGraph(graph_id="g1", name="a")
    g.entities["e1"] = Entity(entity_id="e1", name="A", type=EntityType.CONCEPT)
    g.relations["r1"] = Relation(relation_id="r1", source_id="e1", target_id="e1", type=RelationType.KNOWS)
    s.put(g)
    assert s.delete_graph("g1") is True
    s2 = GraphStore(db)
    assert s2.get("g1") is None


def test_version_increments_persist(tmp_path) -> None:
    db = str(tmp_path / "kg.db")
    s = GraphStore(db)
    g = KnowledgeGraph(graph_id="g1", name="a", version=3)
    s.put(g)
    s2 = GraphStore(db)
    assert s2.get("g1").version == 3
