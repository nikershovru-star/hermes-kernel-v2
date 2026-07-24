# ADR-025 — Knowledge Graph & Semantic Memory

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.11.0)
- **Depends on:** ADR-017 (Event Platform), ADR-019 (Workflow Runtime), ADR-024 (Dynamic Planner)

---

## Context

The v5 Capability Platform needs durable semantic memory: agents should be able
to *remember* facts (entities + relations) and *recall* them later, and the
runtime should be able to reason over that graph (neighbour/path queries,
similarity, rule-based inference) to enrich workflow execution with relevant
context. Until now the only graph-like structure was ADR-004's in-memory chunk
similarity graph (`kernel/graph.py`), which is pipeline-scoped and not a
first-class semantic store.

Four gaps motivated this release:

1. **No entity/relation store** — facts cannot be persisted as a named graph.
2. **No recall** — `AgentRuntime` has no `remember`/`recall` primitive.
3. **No graph-aware workflow context** — workflows run blind to prior knowledge.
4. **No inference** — the graph is inert; relations never trigger derived facts.

## Decision

- **`kernel/semantic_graph.py`** *(new, isolated module)* — domain models:
  `EntityType`, `RelationType` enums; `Entity`, `Relation`, `KnowledgeGraph`,
  `GraphQuery`, `InferenceRule`, `QueryResult`. **Note:** `kernel/domain.py`
  already defines ADR-004 `Entity`/`Relation` (different shape) and
  `kernel/graph.py` defines an ADR-004 `KnowledgeGraph` (chunk similarity). To
  avoid clobbering those and regressing the existing 504-test baseline, the
  ADR-025 semantic-memory models live in their own axis-clean module and are
  imported as `from kernel.semantic_graph import Entity, Relation, ...`.
- **`kernel/events.py`** — 6 KG events (DomainEvent convention): `EntityDiscovered`,
  `RelationCreated`, `GraphUpdated`, `QueryExecuted`, `InferenceFired`,
  `EntityMerged`.
- **`kernel/knowledge_graph.py`** — `KnowledgeGraphEngine` (async):
  - *Lifecycle* — `create_graph` / `get_graph` / `delete_graph` / `list_graphs`.
  - *Entities* — `add_entity` (dedupe by case-insensitive name → merge
    properties + bump confidence), `get_entity`.
  - *Relations* — `add_relation` (validates both endpoints exist).
  - *Traversal* — `get_neighbors` (depth-1, optional `relation_type` filter),
    `find_path` (BFS shortest path, `max_depth` default 5).
  - *Queries* — `query` dispatches by `query_type`: `entity_by_name`
    (case-insensitive substring), `neighbors`, `path`, `similar`
    (cosine via injected `embedding_fn`, else Jaccard token overlap),
    `inference`.
  - *Inference* — `run_inference(rules, max_iterations=3)`: for each enabled
    rule, scan for `pattern` matches (`source_type` + `relation` + `target_type`)
    and apply `action` (`create_relation` [idempotent `similar_to`],
    `merge_entities`, `raise_alert`). Bounded iteration prevents cycles.
  - *Merge* — `merge_entities` redirects relations + emits `EntityMerged`.
  - *Injectables* — `event_bus`, `event_store`, `embedding_fn`, `clock`,
    `sleep`, `rng`. Axis: imports only `kernel.semantic_graph` + `kernel.events`.
- **`kernel/graph_store.py`** — `GraphStore`: in-memory CRUD + optional SQLite
  (`graphs`, `entities`, `relations` tables), mirroring `PlanStore`/`SwarmStore`;
  `delete_graph` cascades.
- **Integration (backward-compatible, all default `None`):**
  - `AgentRuntime(knowledge_graph=…)` + `remember(agent_id, fact)` /
    `recall(agent_id, query)` (lazy per-agent default graph).
  - `WorkflowEngine(knowledge_graph=…)` + `execute_with_context(instance_id,
    workflow, context_graph_id)` — stamps matching entity_ids into
    `workflow.context["kg_matches"]` before executing.
  - `PlanStep.context_graph_id` (optional) — lets the DynamicPlanner executor
    query a graph for relevant entities (ADR-024 hook).
- **No new dependency** — pure asyncio + stdlib; embeddings are injected.

## Consequences

- **Positive:** agents gain persistent semantic memory; workflows become
  context-aware; rule-based inference derives new relations/merges.
- **Positive:** 46 new tests, total **551 passed, 3 skipped**, kernel **91%**;
  `knowledge_graph.py` 91%, `graph_store.py` 100%, `semantic_graph.py` 100%;
  tach green.
- **Positive:** zero regression — all 504 pre-ADR-025 tests pass; KG is opt-in.
- **Negative:** the ADR-025 models are in a *separate* module from ADR-004's
  `Entity`/`Relation` (documented above) — a minor naming duplication by design.

## Honest Notes (known limitations)

- **No real vector DB** — similarity is either a mock/injected embedding or
  Jaccard token overlap. Production scale needs `faiss`/`pgvector`.
- **Inference is rule-based pattern matching**, not LLM reasoning or SPARQL.
- **Graph is local SQLite only** — no distributed graph (would need Neo4j/Dgraph).
- **Pathfinding is BFS shortest path**, not weighted A* / Dijkstra.
- **Entity deduplication is by exact name match**, not fuzzy/phonetic clustering.
- **No temporal reasoning** — relations have no validity intervals.
- **`create_relation` inference is idempotent** (skips an existing `similar_to`
  between the same pair) so repeated runs converge.
