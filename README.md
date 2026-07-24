# Hermes Kernel v2

> An async-first, event-driven **AI Operating System** kernel — Clean
> Architecture, plugin-extensible, MCP-native.

**Status:** v2.10.0 · **504 passed, 3 skipped, 91% coverage** (Python 3.11+).
All phases P0–P5 + extensions A/A2/B/C/D/E/F + ADR-007..023 + CI axis-gate delivered.

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

**Durable sessions (resumable):** pass a `PersistenceRegistry` to the
constructor. Every server→client SSE frame is persisted (per-session workspace
`mcp:<session_id>`) and replayed on reconnect when the client sends a
`Last-Event-ID` header — surviving disconnects and (with a file-backed store) a
full server restart.

```python
from kernel.persistence import PersistenceRegistry
srv = MCPServerStreamable(tool_registry, event_bus,
                          persistence=PersistenceRegistry(db_path="mcp.db"))
srv.set_handler("echo", lambda a: f"got:{a.get('v')}")
srv.start(host="127.0.0.1", port=8080)   # POST /mcp/v1/messages  +  GET /mcp/v1/events
```

### B — KnowledgeRetrievalService (`kernel/retrieval.py`)
Durable, workspace-scoped vector search with pluggable backends (ADR-009).

```python
from kernel.retrieval import KnowledgeRetrievalService
from kernel.retrieval_backends import FaissBackend

svc = KnowledgeRetrievalService(
    persistence, bus,
    backend=FaissBackend(persist_dir=".hermes/faiss", embedding_dim=384)
)
await svc.index_and_persist(node)
top = await svc.query(embedding, workspace_id="ws1", top_k=5)
```

| Backend | Class | Use case | Install |
|---------|-------|----------|---------|
| Memory (default) | `MemoryBackend` | Small corpora, zero-dep | — |
| Faiss | `FaissBackend` | Large corpora, fast ANN | `pip install faiss-cpu` |
| SQLite-VSS | `SQLiteVSSBackend` | Medium corpora, SQLite-native | `pip install sqlite-vss` |

Backends are swappable at construction time; the public API
(`query(embedding, workspace_id, top_k)`) never changes.

### C — Plugin SDK CLI (`plugins/sdk/cli.py`)
A `hermes` console script (installed via `[project.scripts]`):

```bash
hermes plugin init myplugin     # scaffold myplugin.py + plugin.yaml
hermes plugin watch ./plugins   # hot-reload changed modules (polling, no watchdog)
hermes plugin list              # list loaded plugins (name, version, caps, status)
hermes plugin validate ./myplugin   # static check: manifest + compile + (--strict) deps
hermes plugin disable myplugin  # unload from sys.modules + emit plugin.disabled event
```

The `list` / `disable` commands drive the kernel's own `PluginRegistry`
(`kernel/registry.py`) — there is exactly one registry. `validate` runs the
`PluginValidator` (manifest schema → `py_compile` → dependency resolution) with
**no plugin execution**. See **ADR-010**.

### D — Desktop Control builtin plugin (`plugins/builtin/desktop_control/`)

Exposes host mouse / keyboard / screenshot control as kernel Tools under the
`hermes.desktop` capability (see **ADR-011**). Optional deps are installed via
the `desktop` extra:

```bash
pip install 'hermes-kernel-v2[desktop]'
```

| Tool | Params | Returns |
|------|--------|---------|
| `mouse_move` | `x: int, y: int` | `{"ok": bool}` |
| `mouse_click` | `button: str="left", clicks: int=1` | `{"ok": bool}` |
| `key_press` | `key: str` | `{"ok": bool}` |
| `type_text` | `text: str, interval: float=0.01` | `{"ok": bool}` |
| `screenshot` | `region: list[int] | None = None` | `{"image": str}` (base64 PNG) |

Design: `DesktopControlPlugin(BasePlugin)`; every tool is `async` and offloads
the blocking `pyautogui` call via `asyncio.to_thread` (event loop never blocked).
`load()` raises `RuntimeError` on unsupported platforms or missing deps. Tools
are registered into `ToolRegistry` via `register_tools(tr)` after `load()`
(import-side-effect free — no global SDK state needed).

### E — MCP Streamable HTTP hardening (`mcp/server_streamable.py`)

Two durability / interoperability hardenings on top of ADR-008 (see **ADR-012**):

- **Session TTL / eviction.** `MCPServerStreamable(..., session_ttl=86400,
  evict_interval=3600)` starts a background task that deletes persisted
  `McpSessionEvent` rows older than `session_ttl` from each `mcp:<session_id>`
  workspace (file-backed logs no longer grow unbounded). `session_ttl=0`
  disables eviction.
- **`Mcp-Protocol-Version` negotiation.** Both `POST /mcp/v1/messages` and
  `GET /mcp/v1/events` read the client's `Mcp-Protocol-Version` header. A match
  (or its absence — legacy client) is accepted and the server echoes its version
  on every response; a mismatch yields `426 Upgrade Required` advertising the
  supported version. Default server version: `2024-11-05`.

### F — Human Emulation Layer (`plugins/builtin/human_emulation/`, ADR-013)

Autonomous, human-like automation (browse sites, click, type) when the user is
away. Builtin plugin exposing 8 Tools under `hermes.human.browser` /
`hermes.human.input`:

- **`BrowserAgent`** — async Playwright wrapper (visible browser): `browser_start`
  / `navigate` / `click` / `type` (human WPM + rare typos) / `screenshot` / `close`.
- **`InputSimulator`** — pyautogui with human-like micro-delays + occasional
  typos; `FAILSAFE=True` (cursor-to-corner aborts).
- **`HumanProfile`** domain entity — the "digital twin" (typing speed, delays,
  screen resolution, user agent). `BrowserSession` + `ActionLog` entities give a
  full audit trail (workspace-isolated, ADR-007).

Optional deps behind the `[human]` extra (`playwright`, `pyautogui`); the kernel
imports cleanly without them (lazy import + clear `RuntimeError`).

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

Expected: **209 passed, 3 skipped** (sqlite-vss has no Windows wheels → 3
retrieval-backend tests skip; they pass on Linux).

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
- [ADR-009 — Retrieval Backends](docs/adr/ADR-009-retrieval-backends.md) —
  Memory / Faiss / SQLite-VSS pluggable backends behind a stable API.
- [ADR-010 — Plugin CLI UX](docs/adr/ADR-010-plugin-cli-ux.md) —
  `list` / `validate` / `disable` driving the single `PluginRegistry`.
- [ADR-011 — Desktop Control](docs/adr/ADR-011-desktop-control.md) —
  builtin `DesktopControlPlugin` exposing mouse/keyboard/screenshot as
  `hermes.desktop` Tools (lazy `pyautogui`/`Pillow`, async, platform-guarded).
- [ADR-012 — MCP Streamable HTTP hardening](docs/adr/ADR-012-mcp-streamable-hardening.md) —
  `McpSessionEvent` TTL eviction (background task) + `Mcp-Protocol-Version`
  negotiation (426 on mismatch).
- [ADR-013 — Human Emulation Layer](docs/adr/ADR-013-human-emulation.md) —
  `plugins.builtin.human_emulation`: Playwright `BrowserAgent` + pyautogui
  `InputSimulator` + `HumanProfile`/`BrowserSession`/`ActionLog` entities.
- [ADR-016 — Agent/Plugin Unification](docs/adr/ADR-016-agent-unification.md) —
  `BaseAgent` async lifecycle + `AgentRuntime`, unified `Artifact`
  (`format`/`provenance`/`content: Any`), `CapabilityExecutor` (namespaced
  dispatch → `Artifact`).
- [ADR-017 — Event Platform + Desktop Agent Vision](docs/adr/ADR-017-event-platform-cqrs.md) —
  `kernel/events.py` (DomainEvent extends Event + EventStore append-only + CQRS
  Command/Query Bus), `DesktopAgent(BaseAgent)` event-driven, `DesktopVision`
  (OCR + element detection), `CapabilityExecutor.register_agent`.
- [ADR-018 — Capability Handler Auto-Discovery](docs/adr/ADR-018-capability-autodiscovery.md) —
  `kernel/discovery.py` + `CapabilityExecutor.autodiscover(instances)`: reflects
  over loaded plugin/agent instances and wires their capabilities (no plugin
  import, axis clean). Replaces manual bootstrap wiring.
- [ADR-019 — Workflow Runtime Foundation](docs/adr/ADR-019-workflow-runtime.md) —
  `kernel/workflow.py` (`WorkflowEngine` state machine: retry/backoff,
  reverse-order compensation, human-approval PAUSE, input-mapping from prior
  steps), `kernel/planner.py` (goal→`Workflow`), `Workflow` domain model
  replaces the stub + activates dead `Task.workflow_id`. 23 tests.
- [ADR-020 — Execution Sandbox](docs/adr/ADR-020-sandbox.md) —
  `kernel/sandbox.py` (`Sandbox.run` soft timeout/resource enforcement,
  `TimeoutGuard`, `ResourceMonitor` via optional `psutil`); optional integration
  into `AgentRuntime`/`WorkflowEngine`. 17 tests.
- [ADR-021 — Health & Recovery](docs/adr/ADR-021-health-recovery.md) —
  `kernel/health.py` (`HealthMonitor` liveness probes, `DeadLetterQueue` for
  failed work, `CircuitBreaker` per-capability state machine, `RecoveryEngine`
  auto-restart/escalation); optional integration into `AgentRuntime`/
  `WorkflowEngine`/`CapabilityExecutor` (backward-compatible). 40 tests.
- [ADR-022 — Behavior Engine](docs/adr/ADR-022-behavior-engine.md) —
  `plugins/builtin/desktop_control/behavior.py` (`BehaviorEngine`: Bezier mouse
  curves + overshoot, scroll momentum, WPM typing rhythm with typos, gaze
  fixation + reading saccades/regressions) + `human_profile.py`
  (`HumanProfileStore` CRUD + SQLite); optional integration into `DesktopAgent`
  (`desktop.click/type/scroll/read`), `UIElement.center*` for targeting.
  35 tests.
- [ADR-023 — Swarm / Teams](docs/adr/ADR-023-swarm-teams.md) — multi-agent
  orchestration + distributed health: `kernel/swarm.py` (`SwarmCoordinator`:
  create/join/leave, Bully leader election, heartbeat + suspicion/failure,
  capability-aware least-load delegation), `kernel/distributed_health.py`
  (`DistributedHealthMonitor`), `kernel/team_manager.py` (`TeamManager`),
  `kernel/swarm_store.py` (`SwarmStore` in-memory + SQLite); 8 swarm events;
  optional backward-compatible integration into `AgentRuntime`/
  `WorkflowEngine`/`CapabilityExecutor`. 49 tests.
- [ADR-024 — Dynamic Planner](docs/adr/ADR-024-dynamic-planner.md) — adaptive
  replanning + risk-aware execution: `kernel/dynamic_planner.py`
  (`DynamicPlanner`: DAG toposort, retry w/ exponential backoff, rule-based
  replan for 5 triggers + optional LLM shim, `risk_assess`), `kernel/plan_store.py`
  (`PlanStore` in-memory + SQLite), 6 planner events, `WorkflowEngine.execute_adaptive`
  + `SwarmCoordinator.rebalance_load`. 39 tests.

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

## CI / Dependency axis gate

GitHub Actions (`.github/workflows/ci.yml`) runs on every push/PR to `main`,
matrix Python 3.11 + 3.12. Three gates, in order:

| Gate | Command | Threshold |
|------|---------|-----------|
| Axis (Clean Architecture) | `python -m tach check` | zero violations |
| Tests | `python -m pytest tests/` | 228 passed, 0 failed |
| Coverage | `pytest --cov --cov-fail-under=85` | ≥ 85% |

The axis contract (in `[tool.tach]` in `pyproject.toml`):

```
kernel.domain  →  []                      (shared pydantic contract, leaf)
kernel        →  [kernel.domain]
plugins       →  [kernel, kernel.domain]
plugins.builtin.desktop_control  →  [kernel, kernel.domain, plugins]   # explicit submodule
mcp           →  [kernel, kernel.domain]
tests / docs  →  excluded
```

`kernel` never imports `plugins` (the `load_paths` loader is injected, not
imported). `kernel.domain` is the common contract everyone may depend on.
Explicit submodules (e.g. `plugins.builtin.desktop_control`) are declared so
tach enforces the boundary transitively (submodules are not auto-inherited).

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

Local run:

```bash
python -m pip install -e ".[dev]"
python -m tach check     # dependency axis
python -m pytest tests/  # full suite
```

---

## License

Internal / unreleased.
