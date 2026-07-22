# Hermes Kernel v2

> An async-first, event-driven **AI Operating System** kernel — Clean
> Architecture, plugin-extensible, MCP-native.

**Status:** P0–P3 delivered · **113 passed, 0 failed, 90% coverage**
(Python 3.11+).

---

## What is this?

Hermes Kernel v2 is the core runtime of an AI Operating System. It provides the
minimal, well-factored primitives that higher layers (knowledge pipeline,
agents, integrations) build on:

- **Domain** — pure Pydantic entities (documents, chunks, tools, tasks, events,
  capabilities, agents), JSON/MCP-serialisable, zero framework coupling.
- **Event bus** — async, fire-and-forget `publish` + `wait_for` sync barrier,
  fault-contained per handler.
- **Registries** — plugins, tools, capabilities, agents (async lock + sync
  fast-path for construction).
- **Executor** — capability-driven `Task` state machine over the bus.
- **Workspace** — multi-workspace registry (single-tenant today).
- **Plugins** — fault-tolerant loader + a declarative SDK (`@agent`, `@tool`,
  `@on_event`, `@capability`).
- **MCP** — bidirectional Model Context Protocol (client consumes external
  servers; server exposes kernel tools), stdio JSON-RPC 2.0, no external MCP lib.

Architecture follows **Clean Architecture** with a CI-enforced inward dependency
axis (`import-linter`). See [`docs/adr/`](docs/adr/).

---

## Knowledge Pipeline (P2)

Raw files become a queryable knowledge graph through five independent,
event-driven, workspace-scoped stages — each subscribes to the previous stage's
event and publishes its own (no direct coupling). See
[ADR-004](docs/adr/ADR-004-knowledge-pipeline.md).

```
 ┌───────────────┐  document.scanned   ┌────────────────┐  document.parsed
 │  FileScanner  │ ──────────────────▶ │ DocumentParser │ ─────────────────┐
 └───────────────┘                     └────────────────┘                  │
                                                                           ▼
 ┌────────────────┐  chunk.embedded   ┌───────────────┐  chunk.created  ┌──────────────────┐
 │ KnowledgeGraph │ ◀──────────────── │ ChunkEmbedder │ ◀────────────── │  DocumentChunker │
 └───────┬────────┘                   └───────────────┘                 └──────────────────┘
         │ graph.updated
         ▼
   (node_id, edges, workspace_id)
```

| Stage | Does | Optional dep |
|-------|------|--------------|
| `FileScanner` | polling watch, emits scanned files | — (no watchdog) |
| `DocumentParser` | extract text by MIME type | `pdfminer.six` (PDF) |
| `DocumentChunker` | sliding-window chunks w/ overlap | — |
| `ChunkEmbedder` | vector embeddings | `sentence-transformers` |
| `KnowledgeGraph` | cosine-similarity linking, workspace-isolated | — |

Heavy deps are lazily imported and optional; the default path (hash embeddings,
text parsing) is stdlib-only. End-to-end verified in
`tests/test_integration_p2.py` (real `.md` → 10 nodes, 8 edges).

---

## Persistence & Multi-tenancy (P5)

The kernel is multi-tenant: users authenticate, operations are gated by RBAC,
and state survives restart in a workspace-isolated store. All stdlib
(`hashlib`, `sqlite3`) — no heavy deps. See
[ADR-005](docs/adr/ADR-005-multi-tenancy.md).

| Layer | Module | Responsibility |
|-------|--------|----------------|
| Auth | `kernel/auth.py` | `User`, `AuthRegistry`, pbkdf2-hash passwords (no plaintext) |
| RBAC | `kernel/rbac.py` | `Permission`, `Role`, `require_permission` guard |
| Persistence | `kernel/persistence.py` | SQLite async CRUD, workspace-isolated JSON store |

RBAC is a **guard** (`require_permission` before a registry call), not a
wrapper — see `tests/test_rbac.py`. Persistence composes with `WorkspaceRegistry`
(save/load), `KnowledgeGraph` (persist/load) and `FileScanner` (DB-backed
de-duplication) without rewriting them.

---

## Quick start

```bash
# 1. create / activate a Python 3.11+ virtualenv, then install
python -m pip install -e .

# 2. run the test suite
python -m pytest tests/ -v

# 3. run with coverage
python -m pytest tests/ --cov=kernel --cov=plugins --cov=mcp --cov-report=term-missing
```

Expected: **113 passed**.

> **Note:** always invoke pytest as `python -m pytest` so it resolves against the
> active venv interpreter (a bare `pip`/`pytest` may bind to a different Python).

---

## Repository layout

```
hermes-kernel-v2/
├── kernel/            # core (domain, bus, registry, capability, executor, workspace)
├── plugins/
│   ├── base.py        # BasePlugin ABC
│   ├── loader.py      # fault-tolerant plugin loader
│   ├── sdk/           # declarative authoring SDK (@agent/@tool/@on_event/@capability)
│   └── builtin/       # example plugins (filesystem)
├── mcp/               # client.py, server.py, tools.py (MCPToolAdapter)
├── tests/             # 113 tests across 13 suites
├── docs/
│   ├── adr/           # architectural decision records
│   └── roadmap.md     # phase status + coverage
└── pyproject.toml
```

---

## Architecture

Decisions are recorded as ADRs:

- [ADR-001 — Kernel Architecture](docs/adr/ADR-001-kernel-architecture.md) —
  layers, Clean Architecture, async/lock, event-driven.
- [ADR-002 — Plugin System](docs/adr/ADR-002-plugin-system.md) —
  `BasePlugin`, loader, `plugin.yaml` manifest, Plugin SDK.
- [ADR-003 — MCP Integration](docs/adr/ADR-003-mcp-integration.md) —
  MCP client/server, `MCPToolAdapter`.
- [ADR-004 — Knowledge Pipeline](docs/adr/ADR-004-knowledge-pipeline.md) —
  5 event-driven stages, workspace isolation, similarity linking.
- [ADR-005 — Multi-tenancy](docs/adr/ADR-005-multi-tenancy.md) —
  Auth (P5.1), RBAC (P5.2), Persistent Storage (P5.3).

---

## Roadmap

See [docs/roadmap.md](docs/roadmap.md) for full phase status.

| Phase | Name | Status |
|-------|------|--------|
| P0 | Kernel Core | ✅ |
| P1 | Runtime + Capability | ✅ |
| P2 | Knowledge Pipeline | ✅ |
| P3 | MCP + SDK | ✅ |
| P4 | MCP Server | ✅ (SSE pending) |
| P5 | Multi-tenancy | ✅ |

---

## Authoring a plugin

```python
from plugins.sdk import sdk, configure_sdk
from kernel.bus import EventBus
from kernel.registry import AgentRegistry, ToolRegistry
from kernel.capability import CapabilityRegistry

tr = ToolRegistry()
configure_sdk(
    agent_registry=AgentRegistry(),
    tool_registry=tr,
    capability_registry=CapabilityRegistry(tr),
    bus=EventBus(),
)

@sdk.agent(name="researcher", capabilities=["hermes.search"])
class Researcher:
    @sdk.tool(name="web_search", capability="hermes.search",
              schema={"type": "object", "properties": {"q": {"type": "string"}}})
    async def search(self, q: str) -> list:
        ...

Researcher()  # registration happens on construction
```

---

## License

Internal / unreleased.
