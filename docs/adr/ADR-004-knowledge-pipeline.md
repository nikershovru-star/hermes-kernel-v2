# ADR-004 — Knowledge Pipeline

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md)

## Context

The kernel needs to turn raw files into a queryable knowledge graph. This is a
multi-stage transformation (discover → extract → split → embed → link) where
each stage has a distinct concern, distinct failure modes, and distinct optional
dependencies. Coupling them into one monolith would make the pipeline
untestable and force every deployment to carry heavy libs (PDF, ML models).

## Decision

Implement the pipeline as **five independent, event-driven stages** on the
`EventBus`. Each stage subscribes to the previous stage's output event and
publishes its own — no stage holds a direct reference to another. All stages are
**workspace-scoped**: `workspace_id` rides in every event payload and is never
lost.

### Event flow

```
FileScanner ──publish──▶ document.scanned
                            │ (path, mime_type, workspace_id)
                            ▼
DocumentParser ──────────▶ document.parsed
                            │ (path, content, mime_type, workspace_id)
                            ▼
DocumentChunker ─────────▶ chunk.created   (one event per chunk)
                            │ (chunk_id, document_path, text, embedding=None, workspace_id)
                            ▼
ChunkEmbedder ───────────▶ chunk.embedded
                            │ (chunk_id, embedding, workspace_id)
                            ▼
KnowledgeGraph ──────────▶ graph.updated
                              (node_id, edges, workspace_id)
```

### Stages

| # | Stage | Module | Subscribes | Publishes | Cov |
|---|-------|--------|-----------|-----------|-----|
| 1 | `FileScanner` | `kernel/scanner.py` | — (polling) | `document.scanned` | 96% |
| 2 | `DocumentParser` | `kernel/parser.py` | `document.scanned` | `document.parsed` | 84% |
| 3 | `DocumentChunker` | `kernel/chunker.py` | `document.parsed` | `chunk.created` | 94% |
| 4 | `ChunkEmbedder` | `kernel/embedder.py` | `chunk.created` | `chunk.embedded` | 84% |
| 5 | `KnowledgeGraph` | `kernel/graph.py` | `chunk.embedded` | `graph.updated` | 96% |

### Key design points

- **Polling scanner (no watchdog).** `FileScanner` polls on an interval instead
  of pulling in a filesystem-watch library — keeps the dependency graph clean
  and behaviour deterministic/testable. `scan_once()` also supports one-shot
  batch drives (used by the integration test).
- **Optional heavy dependencies, lazily imported.**
  - Parser: `pdfminer.six` for PDF, imported inside the method; absent → graceful
    placeholder `[pdf:name]`. Text (`.md/.txt/.csv/.html/.json`) is stdlib-only.
  - Embedder: `sentence-transformers` backend, lazily imported; absent → falls
    back to the `hash` backend. CI runs entirely on the dependency-free path.
- **Deterministic hash embedding.** Default backend expands a SHA-256 digest to a
  fixed 64-dim, L2-normalised vector. Same text → same vector, so tests and CI
  are reproducible without ML weights.
- **Similarity linking + workspace isolation (ADR-007).** The graph links nodes
  whose embeddings have `cosine ≥ similarity_threshold` (default 0.8), creating
  symmetric `similar_to` `Relation` edges. `find_similar` filters by
  `workspace_id`, so identical content in different workspaces never cross-links.
- **Fault containment.** Every stage's event handler wraps its work in
  try/except: one bad file/chunk is logged and skipped, the pipeline keeps
  flowing (verified by the EventBus per-handler isolation in ADR-001).

## Consequences

**Positive**

- Stages are independently testable (5 unit suites) and independently
  replaceable (swap the embedder backend without touching the graph).
- End-to-end verified: a live `.md` file drives scanner→graph producing a real
  graph (integration test: 10 nodes, 8 edges, `workspace_id` intact across all
  5 hops). See `tests/test_integration_p2.py`.
- Zero heavy dependencies required for the core path; production opts in.

**Negative / trade-offs**

- Polling has latency (interval-bound) vs. event-based FS notification.
- In-memory graph only — persistence (vector DB, `KnowledgeRetrievalService`) is
  a future concern, not owned by the kernel.
- Hash embeddings are not semantically meaningful — they exist for determinism;
  real semantic search requires the `sentence-transformers` backend.

## Related

- [ADR-001 — Kernel Architecture](ADR-001-kernel-architecture.md)
- [Roadmap](../roadmap.md)
