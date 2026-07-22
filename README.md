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

## Extensions: SSE, Streamable HTTP, Retrieval, CLI (post-v0.7.0)

### A — SSE transport for MCP (`mcp/server_sse.py`)
`MCPServerSSE` bridges the stdio `MCPServer` JSON-RPC core to the SSE transport:
`GET /sse` opens a `text/event-stream` and emits an `endpoint` event;
`POST /messages/?sessionId=...` carries JSON-RPC, responses stream back. Pure
stdlib (`http.server` + a dedicated asyncio loop) — no FastAPI/uvicorn.

```python
srv = MCPServerSSE(tool_registry, event_bus)
srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
srv.start(host="127.0.0.1", port=8080)   # GET /sse  +  POST /messages/
```

### A2 — Streamable HTTP transport for MCP (`mcp/server_streamable.py`)
The modern MCP HTTP shape (see [ADR-008](docs/adr/ADR-008-streamable-http.md)):
`POST /mcp/v1/messages` carries JSON-RPC and returns the response **in the POST
body** (HTTP 200); `GET /mcp/v1/events` is a server→client SSE stream for
notifications. Sessions use the `Mcp-Session-Id` header (not a query param),
and JSON-RPC batches (`requests: [...]`) are aggregated. Pure stdlib.

```python
srv = MCPServerStreamable(tool_registry, event_bus)
srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
srv.start(host="127.0.0.1", port=8080)   # POST /mcp/v1/messages  +  GET /mcp/v1/events
```

### B — KnowledgeRetrievalService (`kernel/retrieval.py`)
Durable, workspace-scoped cosine vector search over `KnowledgeNode` embeddings
stored in `PersistenceRegistry`. Keeps an in-memory index for fast ranking and
auto-indexes new nodes on the `graph.updated` event.

```python
svc = KnowledgeRetrievalService(persistence, bus)
await svc.index_and_persist(node)
top = svc.query(embedding, workspace_id="ws1", top_k=5)  # [(node_id, score), ...]
```

### C — Plugin SDK CLI (`plugins/sdk/cli.py`)
A `hermes` console script (installed via `[project.scripts]`):

```bash
hermes plugin init myplugin     # scaffold myplugin.py + plugin.yaml
hermes plugin watch ./plugins   # hot-reload changed modules (polling, no watchdog)
```

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

Expected: **174 passed**.

> **Note:** always invoke pytest as `python -m pytest` so it resolves against the
> active venv interpreter (a bare `pip`/`pytest` may bind to a different Python).

---

## Repository layout

```
hermes-kernel-v2/
├── kernel/            # core (domain, bus, registry, capability, executor, workspace, retrieval)
├── plugins/
│   ├── base.py        # BasePlugin ABC
│   ├── loader.py      # fault-tolerant plugin loader
│   ├── sdk/           # declarative authoring SDK (@agent/@tool/@on_event/@capability)
│   └── builtin/       # example plugins (filesystem)
├── mcp/               # client.py, server.py, server_sse.py, server_streamable.py, tools.py
├── tests/             # 174 tests across 17 suites
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
- [ADR-007 — Workspace Isolation](docs/adr/ADR-007-workspace-isolation.md) —
  formal data-isolation contract (persistence, graph, retrieval, scanner, RBAC).
- [ADR-008 — Streamable HTTP Transport](docs/adr/ADR-008-streamable-http.md) —
  POST /mcp/v1/messages + GET /mcp/v1/events, `Mcp-Session-Id` header, batches.

---

## Roadmap

See [docs/roadmap.md](docs/roadmap.md) for full phase status.

| Phase | Name | Status |
|-------|------|--------|
| P0 | Kernel Core | ✅ |
| P1 | Runtime + Capability | ✅ |
| P2 | Knowledge Pipeline | ✅ |
| P3 | MCP + SDK | ✅ |
| P4 | MCP Server | ✅ |
| P5 | Multi-tenancy | ✅ |
| A | SSE transport | ✅ |
| B | KnowledgeRetrievalService | ✅ |
| C | Plugin SDK CLI | ✅ |
| D | ADR-007 Workspace Isolation | ✅ |

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
