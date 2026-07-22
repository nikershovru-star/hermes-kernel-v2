# ADR-007 — Workspace Isolation

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md), [ADR-004](ADR-004-knowledge-pipeline.md), [ADR-005](ADR-005-multi-tenancy.md)

## Context

Hermes Kernel v2 is multi-tenant at the data layer (P5: Auth, RBAC, Persistence).
A *workspace* is the isolation boundary: every entity, embedding, graph node and
tool result belongs to exactly one workspace and must never leak into another.

This ADR **formalises** rules that were already applied pragmatically during
P2 (graph) and P5 (persistence, rbac) so future work (KnowledgeRetrievalService,
MCP sessions) has a single source of truth.

## Decision

### 1. Workspace is a first-class scope key
Every persisted and in-memory entity carries `workspace_id`. There is no
"global" shared entity except the `default` workspace (auto-seeded by
`WorkspaceRegistry` only when the registry is empty).

### 2. Persistence isolation (enforced)
`PersistenceRegistry` filters **every** read by `WHERE workspace_id = ?`.
`list(workspace_id)`, `count(workspace_id)` and `load_from_db` helpers never
cross workspaces. `list_all` exists only for admin/restore and is explicitly
named so it is not used on the hot path.

### 3. Graph isolation (enforced)
`KnowledgeGraph` stores `domain = workspace_id` on every `KnowledgeNode` and
links only nodes within the same workspace. Similarity search never crosses
workspaces (verified in `tests/test_graph.py`).

### 4. Retrieval isolation (enforced)
`KnowledgeRetrievalService.query` accepts `workspace_id` and filters the
in-memory embedding index before ranking (verified in
`tests/test_retrieval.py::test_workspace_isolation`).

### 5. Tenant identity propagation (convention)
Callers pass `workspace_id` explicitly. There is no hidden "current tenant"
thread-local — the value is always an argument, so isolation is auditable at
the call site. RBAC (`RBACRegistry`) already gates *permission* to operate in a
workspace; this ADR governs *data* isolation within it.

### 6. Scanner isolation (enforced)
`FileScanner` is constructed with `workspace_id` and only emits events for its
assigned paths; the optional persistence dedup is also scoped by the same id.

## Status Quo (implemented before this ADR was written)

| Layer | Isolation mechanism | Verified by |
|-------|--------------------|-------------|
| Persistence | `workspace_id` column + SQL filter | `tests/test_persistence.py::test_list_workspace_isolated` |
| Knowledge graph | `domain = workspace_id`, same-ws edges only | `tests/test_graph.py` |
| Retrieval | `query(embedding, workspace_id)` filter | `tests/test_retrieval.py::test_workspace_isolation` |
| Scanner | per-workspace `workspace_id` ctor | `tests/test_scanner.py` |
| RBAC | `check_permission(user, perm)` gates ops | `tests/test_rbac.py::test_workspace_rbac_integration` |

## Consequences

- **Good:** a single, documented isolation contract; new stages (retrieval,
  MCP sessions) inherit the rule by copying the pattern. Auditable: every
  cross-workspace query is a deliberate, named call (`list_all`).
- **Bad / cost:** explicit `workspace_id` plumbing on every API; multi-tenant
  queries require N single-workspace calls or `list_all` + post-filter.
- **Future:** if cross-workspace search is ever needed, it MUST go through
  `list_all` + an explicit, logged aggregation step — never a silent JOIN.

## Rejected Alternatives

- **Thread-local / contextvar tenant:** hides the boundary, makes tests and
  debugging harder; rejected in favour of explicit arguments (rule 5).
- **Separate DB per workspace:** stronger isolation but operational overhead;
  the single-table `workspace_id` filter is sufficient at kernel scale.
