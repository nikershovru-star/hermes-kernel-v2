# ADR-009 — Retrieval Backends: Memory, Faiss, SQLite-VSS

- **Status:** accepted
- **Date:** 2026-07-23
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md), [ADR-004](ADR-004-knowledge-pipeline.md), [ADR-007](ADR-007-workspace-isolation.md), [ADR-008](ADR-008-streamable-http.md)

## Context

`KnowledgeRetrievalService` (ADR-008 addendum / `kernel/retrieval.py`) kept all
embeddings in-memory and performed brute-force cosine similarity. This is
correct for small corpora but scales poorly: O(N) query latency, linear RAM
growth, no index persistence across restarts.

The public API (`query(embedding, workspace_id, top_k)`) must not change.
Backends are swappable at construction time; the zero-dep default must remain
runnable out-of-the-box.

## Decision

Introduce `BaseRetrievalBackend` ABC in `kernel/retrieval_backends.py` with
three implementations.

### 1. BaseRetrievalBackend ABC

```python
class BaseRetrievalBackend(ABC):
    async def add(self, node_id, embedding, workspace_id) -> None: ...
    async def query(self, embedding, workspace_id, top_k) -> list[tuple[str, float]]: ...
    async def remove(self, node_id, workspace_id) -> None: ...
    async def clear_workspace(self, workspace_id) -> None: ...
    async def persist(self) -> None: ...  # noop for Memory
    async def load(self) -> None: ...     # noop for Memory
```

### 2. MemoryBackend (default)
Brute-force cosine over `dict[(workspace_id, node_id), embedding]`. Zero
dependencies. Extracted from the original `retrieval.py`.

### 3. FaissBackend
`faiss.IndexIDMap(faiss.IndexFlatIP(dim))` for exact cosine via inner product
on normalized vectors. Optional `IndexIVFFlat` path (`use_ivf=True`) for
approximate search at large scale. One index per workspace (isolation by
construction). Persisted as `{workspace_id}.faiss` + `{workspace_id}.json`
(id map). Lazy import faiss; clear error if not installed.

**Known limitation:** `remove()` rebuilds the workspace index via
`reconstruct()`. This works for `IndexFlatIP` but not for `IVFFlat` (reconstruct
unsupported). Documented; acceptable for v0.9.1.

### 4. SQLiteVSSBackend
Dedicated SQLite DB with `sqlite-vss` extension. Table
`vss_nodes(workspace_id, node_id, embedding)` + per-workspace VSS virtual table.
Native persistence; `persist()` is `VACUUM`. Lazy import `sqlite_vss`.

### 5. KnowledgeRetrievalService refactor
Constructor: `KnowledgeRetrievalService(persistence, bus, backend=None)`.
Default backend: `MemoryBackend()`.
- `index_and_persist(node)` — delegates `backend.add()` + `persistence.save()`.
- `query()` — delegates `backend.query()`.
- `load_from_persistence(workspace_id)` — rebuilds index from persisted nodes.
- Auto-index on `graph.updated` event (loads node via `persistence.get(node_id)`).

### 6. Workspace isolation
Enforced at backend level (no post-query filtering):
- **Memory:** dict key is `(workspace_id, node_id)`.
- **Faiss:** separate `faiss.Index` per workspace.
- **SQLite-VSS:** `WHERE workspace_id = ?` in every query.

## Status Quo (implemented)

| Backend | Module | Tests | Coverage* | Verified |
|---------|--------|-------|-----------|----------|
| Memory | `kernel/retrieval_backends.py` | 6 | 100% | always |
| Faiss | `kernel/retrieval_backends.py` | 8 | ~90% | faiss-cpu installed |
| SQLite-VSS | `kernel/retrieval_backends.py` | 3 | 0%† | skipped |

\* Coverage measured on Windows with `faiss-cpu` installed.
† `sqlite-vss` has no Windows wheels; tests skip cleanly. On Linux with both
deps installed, file coverage reaches ~87%.

Integration: `tests/test_retrieval_backends.py` — 17 passed, 3 skipped (VSS),
service-level tests backend-agnostic (API unchanged).

## Consequences

**Good:** scalable retrieval without API break; zero-dep default preserved;
workspace isolation at backend level; Faiss persistence eliminates
rebuild-on-restart.

**Bad / cost:** optional deps must be documented; Faiss `remove()` on IVF
unsupported; SQLite-VSS platform-limited (no Windows wheels).

**Future:** hybrid backend (Memory hot, Faiss cold); incremental Faiss updates
(`add_with_ids` instead of rebuild); cross-workspace search via `list_all` +
aggregation (ADR-007).

## Rejected Alternatives

- **Single global Faiss index + post-filter:** violates ADR-007 isolation
  contract (silent cross-workspace data in one index).
- **Thread-local backend selection:** hides the dependency, complicates
  testing; rejected in favour of explicit constructor injection.
- **Replace Memory entirely:** would force `faiss-cpu` as required dep;
  rejected to keep the zero-dep default.
