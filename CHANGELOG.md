# Changelog

All notable changes to Hermes Kernel v2 are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/); this project
adheres to **semantic versioning** (MAJOR.MINOR.PATCH).

## [v2.6.0] — 2026-07-24 · Execution Sandbox (ADR-020)

### Added
- **`kernel/sandbox.py`** — soft in-process execution sandbox:
  - `Sandbox.run(coro, policy)` wraps any coroutine with a `SandboxPolicy`
  - `TimeoutGuard` (real `asyncio.wait_for` + cancellation)
  - `ResourceMonitor` (best-effort CPU/memory/fd via optional `psutil`)
  - `SandboxError` carrying `SandboxViolation`; emits `SandboxViolationEvent`
    + `SandboxCleanupCompleted` (reuses EventBus + EventStore, ADR-017)
- **`kernel/domain.py`** — `SandboxPolicy`, `SandboxViolation` models
- **`kernel/events.py`** — `SandboxViolationEvent`, `SandboxCleanupCompleted`
- **`kernel/agent.py`** — `AgentRuntime(sandbox=...)` sandboxed `execute()`
  (optional, backward-compatible)
- **`kernel/workflow.py`** — `WorkflowEngine(sandbox=...)` sandboxed steps;
  `SandboxError` routes into retry/compensation
- **`pyproject.toml`** — optional extra `[monitor]` (`psutil>=5.9`)
- **Tests:** `test_sandbox.py`, `test_sandbox_integration.py` (17) — total
  **337 passed, 3 skipped, 89% total coverage**

### Honest notes (deferred)
- Soft/in-process only — no subprocess/container/seccomp isolation → ADR-024
- CPU/mem/fd limits are best-effort sampled, not hard rlimits
- Network/subprocess policy fields are intent flags only (no active blocking)

## [v2.5.0] — 2026-07-23 · Workflow Runtime Foundation (ADR-019)

### Added
- **`kernel/workflow.py`** — `WorkflowEngine` state machine + execution engine:
  - resolves step capabilities via `CapabilityExecutor` / `AgentRuntime`
  - input-mapping from previous step results (`<step>.output.<field>`)
  - retry with exponential backoff (`RetryPolicy`)
  - reverse-order compensation on exhaustion (`WorkflowStep.compensation`)
  - human-approval PAUSE gate (`requires_approval` → `approve()` resume/reject)
  - emits `DomainEvent`s for every transition (reuses `EventBus` + `EventStore`)
- **`kernel/planner.py`** — `Planner` (rule-based goal→`Workflow` via capability
  templates; LLM/reasoning planner deferred to ADR-023)
- **`kernel/domain.py`** — replaced the stub `Workflow` with a full model
  (`WorkflowStep`, `WorkflowInstance`, `WorkflowStatus` enum, `RetryPolicy`,
  `WorkflowTrigger`); **activates the previously-dead `Task.workflow_id` field**
- **`kernel/agent.py`** — `AgentRuntime.execute(agent_id, task, workflow_id=None)`
  now propagates `workflow_id` onto `task.workflow_id`
- **`kernel/events.py`** — 5 new `Workflow*` `DomainEvent` subclasses
- **Tests:** `test_workflow_engine.py` (14), `test_planner.py` (7),
  `test_workflow_events.py` (3) — total **320 passed, 3 skipped, 89.41% cov**

### Honest notes (deferred)
- Planner is rule-based only (no LLM) → ADR-023
- Compensation is reverse-order, not a full Saga → future
- Human approval is in-memory PAUSED state (no external service/UI) → future
- Single-node only (no distributed execution) → v5 Swarm/Teams (ADR-022)
- DAG executed as ordered step list (no parallel/conditional branching yet)

### Added
- **`kernel/discovery.py`** — `discover_handlers(instances, executor)`: post-load
  reflection that wires capability handlers from already-loaded plugin/agent
  instances (no plugin import, kernel→plugins axis preserved):
  - `BaseAgent` instances -> `executor.register_agent` (Task-routing handler).
  - Other instances -> methods marked with `@sdk.tool` become handlers keyed by
    their declared `capability` (signature-adapted to
    `handler(params, context)` via `inspect.signature`).
- **`CapabilityExecutor.autodiscover(instances)`** (`kernel/capability.py`): thin
  convenience wrapper over `discover_handlers`. Replaces the manual
  `register_agent`/`register_handler` bootstrap lines deferred from ADR-017.
- 6 tests (`tests/test_capability_discovery.py`): plugin `@sdk.tool` wiring, param
  forwarding, BaseAgent wiring, mixed instances, idempotency.

### Notes
- `plugins.sdk.tool` is imported **lazily** inside `discover_handlers` to break an
  import cycle (`plugins.sdk` package `__init__` imports `kernel.capability`).
  Documented in ADR-018 so it isn't "tidied" back.
- `get_tools` harvests class-level `@sdk.tool` markers only (instance-assigned
  handlers are not discovered) — consistent with SDK usage.
- Manual `register_agent` / `register_handler` remain available for explicit
  overrides.

## [v2.3.0] — 2026-07-23 · Event Platform + Desktop Agent Vision (ADR-017)

### Added
- **Event Platform foundation** (`kernel/events.py`):
  - `DomainEvent` extends existing `kernel.domain.Event` (aggregate_id,
    timestamp: datetime, version) — flows through the existing async `EventBus`
    without a transport duplicate.
  - `EventStore` — append-only journal (in-memory + optional SQLite table;
    no mutation API → append-only invariant is structural).
  - CQRS: `Command`/`CommandBus` (commands trigger domain logic that emits
    events via `publish_event`), `ReadModel` projections, `Query`/`QueryBus`.
- **DesktopAgent** (`plugins/builtin/desktop_control/desktop_agent.py`,
  `BaseAgent`): event-driven lifecycle. `execute(task)` routes the capability
  through the injected `CommandBus` → pyautogui side-effect (via
  `asyncio.to_thread`) → `DomainEvent` published + appended → returns an
  `Artifact` with a provenance chain of event ids. Emits an event for EVERY
  operation.
- **DesktopVision** (`plugins/builtin/desktop_control/vision.py`): OCR
  (`pytesseract`) + UI element detection (OCR-driven bounding boxes) + fuzzy
  `find_element`. Pure CV (depends only on `kernel.domain`); heavy deps lazy.
- **`CapabilityExecutor.register_agent(agent)`** (`kernel/capability.py`): wires
  a BaseAgent's capabilities as handlers (builds `Task` from params →
  `agent.execute`). Manual wiring for v2.3.0 (auto-discovery → ADR-018).
- **AgentRuntime** (`kernel/agent.py`): now accepts optional `EventBus` +
  `EventStore` and publishes `AgentStarted`/`AgentStopped` on start/stop.
- Desktop domain events (`plugins/builtin/desktop_control/events.py`): typed
  `DomainEvent` subclasses (DesktopScreenshotTaken, DesktopClicked, AgentStarted…).
- `pyproject.toml`: `[desktop-vision]` extra; tach submodule
  `plugins.builtin.desktop_control.vision`; `desktop_control` gains `kernel.events`.
- 31 tests across 4 files (event platform, desktop agent, vision, CQRS); tach
  axis-gate stays green.

### Unchanged
- `DesktopControlPlugin` (legacy BasePlugin / MCP tools) left intact — dual
  surface during transition. `EchoAgent` reference impl unchanged.

## [v2.2.1] — 2026-07-23 · Agent/Plugin Unification (ADR-016)

### Added
- **`BaseAgent`** (async lifecycle ABC, `kernel/agent.py`): `start() -> str`
  (returns `agent_id`), `stop(agent_id) -> bool`, `execute(agent_id, task) ->
  Artifact`, `status(agent_id) -> dict`. Mirrors `BasePlugin` but async (an agent
  *executes* and *returns*).
- **`AgentRuntime`** (`kernel/agent.py`): registry of *active* `BaseAgent`
  instances (start/stop/execute/status/list/get). The runtime counterpart to the
  existing declarative `AgentRegistry` (which `@sdk.agent` populates with
  `Agent` metadata) — same split as `PluginRegistry` vs `PluginManifest`.
- **Unified `Artifact`** (`kernel/domain.py`, extended): added `format: str`,
  `provenance: list[str]`, widened `content: Any` (was `str`). Now versioned,
  linkable, provenance-carrying — answers "where is the screenshot from
  yesterday?" via workspace-scoped persistence.
- **`CapabilityExecutor`** (`kernel/capability.py`, additive): `execute(
  capability, params, context) -> Artifact` resolves a namespaced capability
  (`"browser.navigate"`, `"desktop.click"`) to an **injected** async handler and
  normalizes the result into an `Artifact`. Handlers are injected by the kernel
  (no `kernel -> plugins` import), keeping the axis clean.
- **`plugins/builtin/agents/echo_agent.py`**: `EchoAgent(BaseAgent)` reference
  implementation exercising the full lifecycle without heavy optional deps.
- `pyproject.toml`: explicit `plugins.builtin.agents` tach submodule.
- 14 tests (`tests/test_agent_runtime.py`, `tests/test_artifact.py`,
  `tests/test_capability_executor.py`); `tach` axis-gate stays green.

### Unchanged (delivered earlier)
- **v2.1.0** — Human Emulation Layer (ADR-013): Playwright `BrowserAgent` +
  pyautogui `InputSimulator` + `HumanProfile`/`BrowserSession`/`ActionLog`.
- **v2.0.0** — polish: explicit `plugins.builtin.desktop_control` tach submodule,
  `screenshot` metadata, `CHANGELOG.md`.

## [v2.0.0] — 2026-07-23 · polish release

Maintenance / hardening release. No kernel-API break; all existing tests stay
green (228 passed, 3 skipped, ~87% coverage).

### Added
- Explicit `plugins.builtin.desktop_control` module in `[tool.tach]` axis config
  — protects the Clean Architecture boundary from future dependency regressions
  (tach does not inherit submodules transitively).
- `DesktopControlPlugin.screenshot` now returns metadata-enriched payload:
  `{"format": "png", "encoding": "base64", "image": <str>}` so an MCP client
  knows how to decode without out-of-band assumptions.
- `CHANGELOG.md` (this file).

### Unchanged (delivered in earlier releases)
- **v1.2.0** — MCP Streamable HTTP hardening: Session TTL / eviction (background
  async task trims `McpSessionEvent` older than `session_ttl`, default 24h) and
  `Mcp-Protocol-Version` negotiation (mismatch → `426 Upgrade Required`).
- **v1.1.0** — Desktop Control builtin plugin (`DesktopControlPlugin`): mouse /
  keyboard / screenshot exposed as `hermes.desktop` Tools via `pyautogui` +
  `Pillow` (lazy), `asyncio.to_thread` for blocking calls, platform guard.
- **v1.0.0** — Core kernel stable: P0–P5 + extensions A/A2/B/C/D + ADR-007..011
  + CI axis-gate (tach).

## [v2.1.0] — 2026-07-23 · Human Emulation Layer

### Added
- **Human Emulation Layer (ADR-013)** — builtin plugin under
  `plugins.builtin.human_emulation/` for autonomous, human-like automation:
  - `BrowserAgent` — async Playwright wrapper (visible browser): `browser_start`
    / `navigate` / `click` / `type` (human WPM + rare typos) / `screenshot` /
    `close`, usable as an async context manager. Playwright is a **lazy optional
    dependency** (module import guarded; clear `RuntimeError` if absent).
  - `InputSimulator` — pyautogui wrapper with human-like micro-delays + occasional
    typos; `FAILSAFE = True` (cursor-to-corner aborts). Lazy optional dep.
  - `ProfileManager` — async CRUD over `PersistenceRegistry` for `HumanProfile`
    (workspace-isolated, ADR-007).
  - `HumanEmulationPlugin` — 8 Tools (`browser_start` / `navigate` / `click` /
    `type` / `screenshot` / `close`, `input_mouse_move` / `input_type`), declared
    with `@sdk.tool` and registered explicitly via `register_tools()` (same safe
    pattern as `desktop_control` — no global SDK state at import).
- **Domain entities** (registered in `PersistenceRegistry._TYPE_TO_CLASS`):
  - `HumanProfile` — digital-twin settings (typing speed, typo rate, delays,
    screen resolution, user agent).
  - `BrowserSession` — one browser tab/window; `profile_id` FK, audit fields.
  - `ActionLog` — full audit trail of every emulated action.
- `pyproject.toml`: `[human]` extra (`playwright`, `pyautogui`) + `all` extra
  extended; explicit `plugins.builtin.human_emulation` tach submodule.
- 18 tests (`tests/test_human_emulation.py`, 86% module coverage); Playwright /
  pyautogui mocked — no real browser/desktop in CI.

### Unchanged (delivered in earlier releases)
- **v2.0.0** — polish: explicit `plugins.builtin.desktop_control` tach submodule,
  `screenshot` returns `format`/`encoding` metadata, `CHANGELOG.md` added.
- **v1.2.0** — MCP Streamable HTTP hardening: Session TTL / eviction (background
  async task trims `McpSessionEvent` older than `session_ttl`, default 24h) and
  `Mcp-Protocol-Version` negotiation (mismatch → `426 Upgrade Required`).

## [v1.2.0] — 2026-07-23 · MCP Streamable HTTP hardening

### Added
- `MCPServerStreamable(session_ttl=86400, evict_interval=3600)` — background task
  evicts persisted `McpSessionEvent` rows older than TTL per `mcp:<session_id>`
  workspace (file-backed event logs no longer grow unbounded). `session_ttl=0`
  disables eviction.
- `Mcp-Protocol-Version` negotiation header (`DEFAULT_PROTOCOL_VERSION =
  "2024-11-05"`). Both `POST /mcp/v1/messages` and `GET /mcp/v1/events` read the
  client header; match (or absence = legacy) is accepted and echoed; mismatch
  yields `426 Upgrade Required` advertising the supported version.
- `docs/adr/ADR-012-mcp-streamable-hardening.md`.

## [v1.1.0] — 2026-07-23 · Desktop Control Plugin

### Added
- `plugins/builtin/desktop_control/` — `DesktopControlPlugin(BasePlugin)`
  exposing `mouse_move`, `mouse_click`, `key_press`, `type_text`, `screenshot`
  as `hermes.desktop` Tools (`@sdk.tool` metadata). Tools registered explicitly
  via `register_tools(tool_registry)` + `register_agent(agent_registry)` after
  `load()` (import-side-effect free).
- Lazy optional deps `pyautogui` / `Pillow` (installed via `[desktop]` extra);
  `asyncio.to_thread` for blocking calls; platform guard in `load()`.
- `docs/adr/ADR-011-desktop-control.md`.

## [v1.0.0] — 2026-07-23 · Core kernel stable

### Delivered
- Phases P0–P5: domain model, event bus, plugin registry, persistence,
  capability registry, workspace isolation (ADR-007), retrieval service.
- Extensions A (SSE), A2 (Streamable HTTP + durable sessions, ADR-008),
  B (KnowledgeRetrievalService), C (Plugin SDK CLI, ADR-010), D (builtin
  plugins / retrieval backends, ADR-009).
- CI axis-gate (`tach check` + pytest + coverage ≥85%).
- ADR-001..ADR-010.

---

[Unreleased]: #changelog
[v2.5.0]: #v250--2026-07-23--workflow-runtime-foundation-adr-019
[v2.4.0]: #v240--2026-07-23--capability-handler-auto-discovery-adr-018
[v2.3.0]: #v230--2026-07-23--event-platform--desktop-agent-vision-adr-017
[v2.2.1]: #v221--2026-07-23--agentplugin-unification-adr-016
[v2.1.0]: #v210--2026-07-23--human-emulation-layer
[v2.0.0]: #v200--2026-07-23--polish-release
[v1.2.0]: #v120--2026-07-23--mcp-streamable-http-hardening
[v1.1.0]: #v110--2026-07-23--desktop-control-plugin
[v1.0.0]: #v100--2026-07-23--core-kernel-stable
