"""kernel/graph_store.py — Knowledge-graph persistence (ADR-025).

In-memory CRUD + optional SQLite, mirroring ``PlanStore`` / ``SwarmStore``.
Tables: ``graphs (graph_id TEXT PRIMARY KEY, data TEXT)``,
``entities (entity_id TEXT PRIMARY KEY, graph_id TEXT, data TEXT)``,
``relations (relation_id TEXT PRIMARY KEY, graph_id TEXT, data TEXT)``.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from kernel.semantic_graph import Entity, KnowledgeGraph, Relation


class GraphStore:
    def __init__(self, db_path: str | None = None) -> None:
        self._mem: dict[str, KnowledgeGraph] = {}
        self._mem_entities: dict[str, Entity] = {}
        self._mem_relations: dict[str, Relation] = {}
        self._db = db_path
        if db_path is not None:
            self._init_db()
            self._load_all()

    # -- sqlite ----------------------------------------------------------- #
    def _init_db(self) -> None:
        self._conn = sqlite3.connect(self._db)
        cur = self._conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS graphs (graph_id TEXT PRIMARY KEY, data TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS entities (entity_id TEXT PRIMARY KEY, graph_id TEXT, data TEXT)")
        cur.execute("CREATE TABLE IF NOT EXISTS relations (relation_id TEXT PRIMARY KEY, graph_id TEXT, data TEXT)")
        self._conn.commit()

    def _load_all(self) -> None:
        cur = self._conn.cursor()
        for graph_id, data in cur.execute("SELECT graph_id, data FROM graphs"):
            g = KnowledgeGraph.model_validate_json(data)
            self._mem[g.graph_id] = g
        for entity_id, graph_id, data in cur.execute("SELECT entity_id, graph_id, data FROM entities"):
            e = Entity.model_validate_json(data)
            self._mem_entities[e.entity_id] = e
            if graph_id in self._mem:
                self._mem[graph_id].entities[e.entity_id] = e
        for relation_id, graph_id, data in cur.execute("SELECT relation_id, graph_id, data FROM relations"):
            r = Relation.model_validate_json(data)
            self._mem_relations[r.relation_id] = r
            if graph_id in self._mem:
                self._mem[graph_id].relations[r.relation_id] = r

    # -- graphs ----------------------------------------------------------- #
    def put(self, graph: KnowledgeGraph) -> None:
        self._mem[graph.graph_id] = graph
        if self._db is None:
            return
        cur = self._conn.cursor()
        cur.execute(
            "INSERT OR REPLACE INTO graphs (graph_id, data) VALUES (?, ?)",
            (graph.graph_id, graph.model_dump_json()),
        )
        for e in graph.entities.values():
            cur.execute(
                "INSERT OR REPLACE INTO entities (entity_id, graph_id, data) VALUES (?, ?, ?)",
                (e.entity_id, graph.graph_id, e.model_dump_json()),
            )
        for r in graph.relations.values():
            cur.execute(
                "INSERT OR REPLACE INTO relations (relation_id, graph_id, data) VALUES (?, ?, ?)",
                (r.relation_id, graph.graph_id, r.model_dump_json()),
            )
        self._conn.commit()

    def get(self, graph_id: str) -> KnowledgeGraph | None:
        return self._mem.get(graph_id)

    def delete_graph(self, graph_id: str) -> bool:
        if graph_id not in self._mem:
            return False
        g = self._mem[graph_id]
        for eid in list(g.entities):
            self._mem_entities.pop(eid, None)
        for rid in list(g.relations):
            self._mem_relations.pop(rid, None)
        del self._mem[graph_id]
        if self._db is not None:
            cur = self._conn.cursor()
            cur.execute("DELETE FROM graphs WHERE graph_id = ?", (graph_id,))
            cur.execute("DELETE FROM entities WHERE graph_id = ?", (graph_id,))
            cur.execute("DELETE FROM relations WHERE graph_id = ?", (graph_id,))
            self._conn.commit()
        return True

    def list_graphs(self) -> list[KnowledgeGraph]:
        return list(self._mem.values())
