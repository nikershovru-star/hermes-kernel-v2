"""kernel/knowledge_graph.py — KnowledgeGraphEngine (ADR-025).

Semantic-memory graph: entities + relations, neighbor/path queries, fuzzy &
vector (optional injected ``embedding_fn``) similarity, rule-based inference,
entity merge, and optional SQLite persistence.

AXIS CONTRACT: imports only ``kernel.semantic_graph`` (domain models),
``kernel.events`` (events), and stdlib. Never imports ``plugins/`` or ``mcp/``.
"""

from __future__ import annotations

import asyncio
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from kernel.events import (
    EntityDiscovered,
    EntityMerged,
    EventBus,
    EventStore,
    GraphUpdated,
    InferenceFired,
    QueryExecuted,
    RelationCreated,
)
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _jaccard(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


class KnowledgeGraphEngine:
    def __init__(
        self,
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        embedding_fn: Callable[[str], list[float]] | None = None,
        store: Any = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], Any] = lambda s: asyncio.sleep(0),
        rng: Any = None,
    ) -> None:
        self._bus = event_bus
        self._event_store = event_store
        self._store = store
        self._emb = embedding_fn
        self._clock = clock or _now
        self._sleep = sleep
        self._rng = rng
        self._graphs: dict[str, KnowledgeGraph] = {}
        if store is not None:
            self._graphs = {g.graph_id: g for g in store.list_graphs()}

    # -- emit ------------------------------------------------------------- #
    async def _emit(self, event: Any) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001
                pass

    def _persist(self, graph: KnowledgeGraph) -> None:
        if self._store is not None:
            self._store.put(graph)

    # -- graph lifecycle -------------------------------------------------- #
    async def create_graph(self, name: str) -> KnowledgeGraph:
        g = KnowledgeGraph(graph_id=uuid.uuid4().hex, name=name)
        self._graphs[g.graph_id] = g
        self._persist(g)
        return g

    def get_graph(self, graph_id: str) -> KnowledgeGraph | None:
        return self._graphs.get(graph_id)

    async def delete_graph(self, graph_id: str) -> bool:
        g = self._graphs.get(graph_id)
        if g is None:
            return False
        if self._store is not None:
            self._store.delete_graph(graph_id)
        del self._graphs[graph_id]
        return True

    def list_graphs(self) -> list[KnowledgeGraph]:
        return list(self._graphs.values())

    # -- entities --------------------------------------------------------- #
    async def add_entity(self, graph_id: str, entity: Entity) -> Entity:
        g = self._graphs[graph_id]
        # dedupe by name (case-insensitive)
        for existing in g.entities.values():
            if existing.name.lower() == entity.name.lower():
                existing.properties.update(entity.properties)
                existing.confidence = max(existing.confidence, entity.confidence)
                existing.source = entity.source or existing.source
                g.updated_at = self._clock()
                g.version += 1
                self._persist(g)
                await self._emit(GraphUpdated(g.graph_id, g.version, f"merge:{existing.entity_id}"))
                return existing
        g.entities[entity.entity_id] = entity
        g.updated_at = self._clock()
        g.version += 1
        self._persist(g)
        await self._emit(EntityDiscovered(g.graph_id, entity.entity_id, entity.name, entity.type.value, entity.source, entity.confidence))
        await self._emit(GraphUpdated(g.graph_id, g.version, f"add_entity:{entity.entity_id}"))
        return entity

    def get_entity(self, graph_id: str, entity_id: str) -> Entity | None:
        g = self._graphs.get(graph_id)
        if g is None:
            return None
        return g.entities.get(entity_id)

    # -- relations -------------------------------------------------------- #
    async def add_relation(self, graph_id: str, relation: Relation) -> Relation:
        g = self._graphs[graph_id]
        if relation.source_id not in g.entities or relation.target_id not in g.entities:
            raise ValueError(f"relation endpoints missing in graph {graph_id}")
        g.relations[relation.relation_id] = relation
        g.updated_at = self._clock()
        g.version += 1
        self._persist(g)
        await self._emit(RelationCreated(g.graph_id, relation.relation_id, relation.source_id, relation.target_id, relation.type.value, relation.weight))
        await self._emit(GraphUpdated(g.graph_id, g.version, f"add_relation:{relation.relation_id}"))
        return relation

    # -- traversal -------------------------------------------------------- #
    def get_neighbors(
        self, graph_id: str, entity_id: str, relation_type: RelationType | None = None
    ) -> list[tuple[Entity, Relation]]:
        g = self._graphs.get(graph_id)
        if g is None or entity_id not in g.entities:
            return []
        out: list[tuple[Entity, Relation]] = []
        for r in g.relations.values():
            if r.source_id == entity_id or (r.bidirectional and r.target_id == entity_id):
                if relation_type is not None and r.type != relation_type:
                    continue
                other_id = r.target_id if r.source_id == entity_id else r.source_id
                other = g.entities.get(other_id)
                if other is not None:
                    out.append((other, r))
        return out

    def find_path(
        self, graph_id: str, from_entity_id: str, to_entity_id: str, max_depth: int = 5
    ) -> list[Relation] | None:
        g = self._graphs.get(graph_id)
        if g is None or from_entity_id not in g.entities or to_entity_id not in g.entities:
            return None
        # BFS over adjacency (undirected via bidirectional)
        adj: dict[str, list[Relation]] = {}
        for r in g.relations.values():
            adj.setdefault(r.source_id, []).append(r)
            if r.bidirectional:
                adj.setdefault(r.target_id, []).append(r)
        prev: dict[str, tuple[str, Relation]] = {}
        seen = {from_entity_id}
        queue = [from_entity_id]
        while queue:
            cur = queue.pop(0)
            if cur == to_entity_id:
                break
            for r in adj.get(cur, []):
                nxt = r.target_id if r.source_id == cur else r.source_id
                if nxt not in seen:
                    seen.add(nxt)
                    prev[nxt] = (cur, r)
                    queue.append(nxt)
        if to_entity_id not in prev and from_entity_id != to_entity_id:
            return None
        # reconstruct
        path: list[Relation] = []
        node = to_entity_id
        while node != from_entity_id:
            p, r = prev[node]
            path.append(r)
            node = p
        path.reverse()
        return path

    # -- queries ---------------------------------------------------------- #
    async def query(self, graph_id: str, query: GraphQuery) -> QueryResult:
        start = self._clock()
        g = self._graphs.get(graph_id)
        entities: list[str] = []
        relations: list[str] = []
        inferred: list[str] = []
        qt = query.query_type
        if qt == "entity_by_name":
            name = (query.parameters.get("name") or "").lower()
            for e in (g.entities.values() if g else []):
                if name in e.name.lower():
                    entities.append(e.entity_id)
        elif qt == "neighbors":
            eid = query.parameters.get("entity_id")
            rtype = query.parameters.get("relation_type")
            rt = RelationType(rtype) if rtype else None
            for ent, _ in self.get_neighbors(graph_id, eid, rt):
                entities.append(ent.entity_id)
        elif qt == "path":
            f = query.parameters.get("from_entity_id")
            t = query.parameters.get("to_entity_id")
            md = int(query.parameters.get("max_depth", 5))
            p = self.find_path(graph_id, f, t, md)
            if p:
                relations = [r.relation_id for r in p]
        elif qt == "similar":
            target = query.parameters.get("name") or query.parameters.get("entity_id") or ""
            limit = query.limit
            scored = self._similar(graph_id, target, limit)
            entities = [eid for eid, _ in scored]
        elif qt == "inference":
            rules = query.parameters.get("rules", [])
            res = await self.run_inference(graph_id, rules)
            for r in res:
                entities.extend(r.entities)
                relations.extend(r.relations)
                inferred.extend(r.inferred)
        result = QueryResult(
            result_id=uuid.uuid4().hex,
            query_id=query.query_id,
            entities=entities[: query.limit],
            relations=relations,
            inferred=inferred,
            duration_ms=int((self._clock() - start).total_seconds() * 1000),
        )
        await self._emit(QueryExecuted(query.query_id, graph_id, qt, len(entities) + len(relations), result.duration_ms))
        return result

    def _similar(self, graph_id: str, target: str, limit: int) -> list[tuple[str, float]]:
        g = self._graphs.get(graph_id)
        if g is None:
            return []
        scored: list[tuple[str, float]] = []
        for e in g.entities.values():
            if self._emb is not None:
                score = _cosine(self._emb(target), self._emb(e.name))
            else:
                score = _jaccard(target, e.name)
            scored.append((e.entity_id, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    # -- inference -------------------------------------------------------- #
    async def run_inference(
        self, graph_id: str, rules: list[InferenceRule], max_iterations: int = 3
    ) -> list[QueryResult]:
        g = self._graphs.get(graph_id)
        results: list[QueryResult] = []
        if g is None:
            return results
        active = sorted([r for r in rules if r.enabled], key=lambda r: -r.priority)
        # Bounded iteration prevents infinite loops when a rule's action creates
        # relations that re-match the same pattern (e.g. create_relation on a
        # self-similar pair). Stops early when an iteration adds nothing new.
        for _ in range(max(max_iterations, 1)):
            before = len(g.relations)
            for rule in active:
                matched = self._match_rule(g, rule)
                if not matched:
                    continue
                action_taken = rule.action
                inferred_ids: list[str] = []
                if rule.action == "create_relation":
                    for a, b in matched:
                        # idempotent: skip if a similar_to already links this pair
                        exists = any(
                            r.type == RelationType.SIMILAR_TO
                            and {r.source_id, r.target_id} == {a, b}
                            for r in g.relations.values()
                        )
                        if exists:
                            continue
                        rid = uuid.uuid4().hex
                        rel = Relation(relation_id=rid, source_id=a, target_id=b, type=RelationType.SIMILAR_TO, weight=1.0)
                        g.relations[rid] = rel
                        inferred_ids.append(rid)
                        await self._emit(RelationCreated(g.graph_id, rid, a, b, rel.type.value, rel.weight))
                elif rule.action == "merge_entities":
                    for a, b in matched:
                        canonical = await self.merge_entities(graph_id, a, [b])
                        inferred_ids.append(canonical.entity_id)
                elif rule.action == "raise_alert":
                    pass  # no mutation
                g.updated_at = self._clock()
                self._persist(g)
                await self._emit(InferenceFired(g.graph_id, rule.rule_id, [m for pair in matched for m in pair], action_taken))
                results.append(QueryResult(result_id=uuid.uuid4().hex, query_id=uuid.uuid4().hex, inferred=inferred_ids))
            if len(g.relations) == before:
                break
        return results

    def _match_rule(self, g: KnowledgeGraph, rule: InferenceRule) -> list[tuple[str, str]]:
        pat = rule.pattern
        st = pat.get("source_type")
        rt = pat.get("relation")
        tt = pat.get("target_type")
        pairs: list[tuple[str, str]] = []
        for src in g.entities.values():
            if st and src.type.value != st:
                continue
            for rel in g.relations.values():
                if rel.source_id != src.entity_id:
                    continue
                if rt and rel.type.value != rt:
                    continue
                tgt = g.entities.get(rel.target_id)
                if tgt is None:
                    continue
                if tt and tgt.type.value != tt:
                    continue
                pairs.append((src.entity_id, tgt.entity_id))
        return pairs

    # -- merge ------------------------------------------------------------ #
    async def merge_entities(self, graph_id: str, canonical_id: str, duplicate_ids: list[str]) -> Entity:
        g = self._graphs[graph_id]
        canonical = g.entities[canonical_id]
        merged: list[str] = []
        for dup in duplicate_ids:
            if dup == canonical_id or dup not in g.entities:
                continue
            d = g.entities[dup]
            canonical.properties.update(d.properties)
            canonical.confidence = max(canonical.confidence, d.confidence)
            merged.append(dup)
            # redirect relations
            for r in g.relations.values():
                if r.source_id == dup:
                    r.source_id = canonical_id
                if r.target_id == dup:
                    r.target_id = canonical_id
            del g.entities[dup]
        if merged:
            g.updated_at = self._clock()
            g.version += 1
            self._persist(g)
            await self._emit(EntityMerged(g.graph_id, canonical_id, merged, "duplicate"))
            await self._emit(GraphUpdated(g.graph_id, g.version, f"merge:{canonical_id}"))
        return canonical
