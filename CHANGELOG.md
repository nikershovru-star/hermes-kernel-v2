# Changelog

All notable changes to Hermes Kernel v2 are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/); this project
adheres to **semantic versioning** (MAJOR.MINOR.PATCH).

## [v2.11.0] — 2026-07-24 · Knowledge Graph & Semantic Memory (ADR-025)

### Added
- **`kernel/semantic_graph.py`** — semantic-memory domain models: `EntityType`,
  `RelationType` enums; `Entity`, `Relation`, `KnowledgeGraph`, `GraphQuery`,
  `InferenceRule`, `QueryResult`. Isolated from ADR-004's `Entity`/`Relation`/
  `KnowledgeGraph` (different shapes) to preserve the existing baseline.
- **`kernel/knowledge_graph.py`** — `KnowledgeGraphEngine` (async): `create_graph`,
  `add_entity` (case-insensitive name dedupe → merge + confidence bump),
  `add_relation` (endpoint validation), `get_neighbors` (depth-1, optional
  `relation_type` filter), `find_path` (BFS shortest path), `query` (dispatch:
  `entity_by_name` / `neighbors` / `path` / `similar` [injected embedding_fn or
  Jaccard fallback] / `inference`), `run_inference` (rule pattern match →
  `create_relation` / `merge_entities` / `raise_alert`, bounded `max_iterations=3`),
  `merge_entities`, `delete_graph`. All `event_bus`/`event_store`/`embedding_fn`/
  `clock`/`sleep`/`rng` injectable for determinism. Axis: `kernel.semantic_graph`
  + `kernel.events` only.
- **`kernel/graph_store.py`** — `GraphStore`: in-memory CRUD + optional SQLite
  (`graphs` / `entities` / `relations` tables), `delete_graph` cascades.
- **6 KG events** (`kernel/events.py`): `EntityDiscovered`, `RelationCreated`,
  `GraphUpdated`, `QueryExecuted`, `InferenceFired`, `EntityMerged`.
- **`AgentRuntime`** — `knowledge_graph` param + `remember(agent_id, fact)` /
  `recall(agent_id, query)` (lazy per-agent default graph).
- **`WorkflowEngine`** — `knowledge_graph` param + `execute_with_context(...)`
  (stamps matching entity_ids into `workflow.context["kg_matches"]`).
- **`PlanStep.context_graph_id`** (optional) — ADR-024 executor hook.
- **46 new tests** (knowledge_graph 18, integration 14, store 6, inference 8).

### Honest Notes
- No real vector DB — similarity is injected embedding or Jaccard token overlap.
- Inference is rule-based pattern matching, not LLM/SPARQL.
- Graph is local SQLite only; BFS pathfinding; exact-name dedupe; no temporal
  validity on relations.

## [v2.10.0] — 2026-07-24 · Dynamic Planner (ADR-024)

### Added
- **`kernel/dynamic_planner.py`** — `DynamicPlanner` (async): builds a `Plan`
  (DAG of `PlanStep`), executes steps in topological order with **retry +
  exponential backoff** (injectable `sleep`), and **adaptive replanning** when
  a step exhausts its retry budget. Five rule-based replan triggers:
  `capability_missing` (unassign agent), `agent_unhealthy` (round-robin
  reassign), `step_failed` (naive split into `s1-a`/`s1-b` substeps),
  `risk_escalation` (bump `RiskLevel` + `retry_budget`), `swarm_rebalance`
  (reassign between agents). Optional **LLM shim** (`llm_client` injectable)
  for demo replanning; falls back to rules on any parse error. `risk_assess`
  escalates step risk from past `ExecutionOutcome` history (HIGH after >2
  failures, CRITICAL on `agent_unhealthy`). All clocks/sleep/rng/llm injectable
  for deterministic tests. Axis: imports only `kernel.domain` + `kernel.events`
  (+ lazy `kernel.swarm`).
- **`kernel/plan_store.py`** — `PlanStore`: in-memory CRUD + optional SQLite
  persistence (`plans`, `outcomes` tables), mirroring `SwarmStore`.
- **6 planner events** (`kernel/events.py`): `PlanCreated`, `StepPlanned`,
  `ReplanTriggered`, `PlanAdapted`, `StepExecuted`, `RiskEscalated`.
- **`kernel/workflow.py`** — `WorkflowEngine.execute_adaptive` (DAG execution
  via the planner; transparent fallback to legacy `execute_step` when no planner
  is wired) + `replan_step` (emits `ReplanTriggered`, returns adapted `Plan`).
  Backward-compatible: existing `WorkflowEngine` tests unchanged.
- **`kernel/swarm.py`** — `SwarmCoordinator.rebalance_load`: emits a
  `ReplanTrigger` (reason `swarm_rebalance`) when load variance across healthy
  members exceeds 0.5.
- **39 new tests** (`test_dynamic_planner.py`, `test_dynamic_planner_integration.py`,
  `test_plan_store.py`, `test_planner_workflow_compat.py`).

### Honest Notes
- **LLM replanning is a shim.** The planner serializes the plan/trigger to a
  prompt and asks the LLM for ad-hoc JSON (`{"steps": [...]}`); there is no
  formal schema, no cost/timeout budgeting, and no validation beyond field
  extraction. Use only for demos.
- **Rule-based replan covers ~80%** of realistic failure modes; the LLM path is
  an augmentation, not a replacement.
- **Substep splitting is naive** — `step_failed` splits a step into `-a`/`-b`
  suffixes with halved `estimated_duration_ms`; it does not semantically
  decompose the work.
- **Risk assessment is a heuristic** (failure-count threshold + `error_type`
  lookup), not predictive modeling.
- **Persistence is local SQLite only** — no distributed/cross-node plan store.

## [v2.9.0] — 2026-07-24 · Swarm / Teams (ADR-023)

### Added
- **`kernel/swarm.py`** — `SwarmCoordinator` (async): create/join/leave,
  Bully leader election (healthy member with highest lexicographic `agent_id`;
  `LeaderElected` emitted on leader change), heartbeat tracking + load scores,
  `check_partitions` (suspected past `suspicion_timeout_ms`=3s, unhealthy past
  `failure_timeout_ms`=10s, emits `HeartbeatMissed`/`NodePartitioned`,
  re-elects on leader loss), capability-aware least-load delegation with
  round-robin tie-break (`TaskDelegated`). All timers injectable (clock/sleep/
  rng) for deterministic tests.
- **`kernel/distributed_health.py`** — `DistributedHealthMonitor`: periodic
  `HeartbeatReceived` emission, remote node health via injectable clock;
  composes with `HealthMonitor` (ADR-021).
- **`kernel/team_manager.py`** — `TeamManager`: high-level facade —
  `create_team`, `assign_role`, `disband_team`, `execute_distributed` (batches
  tasks across swarm members, injectable executor).
- **`kernel/swarm_store.py`** — `SwarmStore`: in-memory CRUD + optional SQLite
  (`swarms`, `delegations` tables), mirroring ADR-022 `HumanProfileStore`.
- **`kernel/domain.py`** — `SwarmTopology`, `SwarmMember`, `Swarm`, `NodeInfo`,
  `TaskDelegation`.
- **`kernel/events.py`** — 8 swarm events: `AgentJoinedSwarm`, `AgentLeftSwarm`,
  `HeartbeatReceived`, `HeartbeatMissed`, `LeaderElected`, `TaskDelegated`,
  `TaskCompleted`, `NodePartitioned` (using the `super().__init__(type=…)`
  convention).
- **Optional integration (backward-compatible, all default `None`):**
  - `AgentRuntime(swarm_coordinator=…)` + `join_swarm`/`leave_swarm`; `execute`
    delegates when the capability is missing locally.
  - `WorkflowEngine(swarm_coordinator=…)` + `schedule_swarm`/`execute_step_swarm`.
  - `CapabilityExecutor.discover_remote(swarm_id, coordinator)` — unique healthy
    member capabilities.
- **`kernel/__init__.py`** — exports `SwarmCoordinator`, `TeamManager`,
  `DistributedHealthMonitor`, `Swarm`, `SwarmMember`.

### Honest notes (deferred)
- **Bully, not Raft/Paxos** — simple, deterministic, but not partition-resilient
  across *real* separate nodes (only logical in-proc nodes here).
- **Timeout suspicion, not Phi-accrual** — coarse binary thresholds.
- **Logical multi-node only** — "distributed" means in-proc EventBus routing;
  no TCP/gRPC transport yet.
- **No consensus for state mutation** — EventBus eventual consistency; split-brain
  possible if a partition heals without explicit reconciliation.
- **Round-robin + capability filter**, not weighted least-connections.
- **No automatic task migration** on worker failure — the delegator must retry.
- **Swarm state persistence is local SQLite only** (no cloud sync).
- `HeartbeatMissed`/`NodePartitioned` are keyed on `node_id` (not `swarm_id`), so
  subscribers/replay must query by node aggregate.

## [v2.8.0] — 2026-07-24 · Behavior Engine (ADR-022)

### Added
- **`plugins/builtin/desktop_control/behavior.py`** — `BehaviorEngine`,
  human-like desktop primitives (all async, pyautogui via `asyncio.to_thread`,
  injectable RNG + sleep for deterministic tests):
  - `move_to` / `click` — quadratic Bezier path with overshoot + correction,
    gaze fixation before click
  - `scroll_page` / `scroll_to_element` — momentum (accelerate→coast→
    decelerate) + reading pauses
  - `type_text` — WPM-derived variable intervals, bursts, typo + backspace +
    retype
  - `gaze_at` / `read_text` — saccade + fixation, word-group reading with
    bounded regressions
- **`plugins/builtin/desktop_control/human_profile.py`** — `HumanProfileStore`
  CRUD + optional SQLite persistence for named behavior profiles
- **`kernel/domain.py`** — `BehaviorProfile`, `BehaviorSession`,
  `HumanBehaviorProfile` (named to avoid colliding with ADR-013 `HumanProfile`)
- **`kernel/events.py`** — `MouseMoved`, `MouseClicked`, `Scrolled`, `TextTyped`,
  `GazeFixated`, `ReadingProgress`
- **`vision.py`** — `UIElement.center` / `center_x` / `center_y` +
  `DesktopVision.find_element_for_behavior`
- **Integration (optional, backward-compatible):** `DesktopAgent(behavior=…)`
  routes `desktop.click/type/scroll/read` through the engine; legacy CommandBus
  path preserved when `behavior=None`
- **Tests:** `test_behavior_engine.py` (18), `test_human_profile.py` (10),
  `test_behavior_integration.py` (7) — total **412 passed, 3 skipped, 91% cov**

### Honest notes (deferred)
- Bezier curves approximated (quadratic, not cubic); timing uses uniform (not
  Gaussian) distributions
- Gaze is 2D only (no head/blink); reading uses simple word-split (no NLP)
- No anti-detection beyond timing (no viewport jitter / UA rotation)
- Profile persistence is in-memory + optional SQLite (no cloud sync)

## [v2.7.0] — 2026-07-24 · Health & Recovery (ADR-021)

### Added
- **`kernel/health.py`** — v5 Execution Platform Health/Recovery layer:
  - `HealthMonitor` — periodic liveness probes, one `HealthRecord` per
    component, one cancellable `asyncio.Task` per probe loop; emits
    `AgentUnhealthy` / `AgentRecovered` on status transitions
  - `DeadLetterQueue` — append-only failed-work store; append/list/recover/
    idempotent `replay(handler)`; emits `DeadLetterAppended`/`DeadLetterRecovered`
  - `CircuitBreaker` — per-capability `CLOSED→OPEN→HALF_OPEN→CLOSED` state
    machine (HALF_OPEN admits exactly one test call); emits `CircuitBreakerTripped`
  - `RecoveryEngine` — subscribes to `AgentUnhealthy`; restart agent / dead-letter
    workflow / escalate after bounded max-restarts (no infinite loop)
- **`kernel/domain.py`** — `HealthCheck`, `HealthStatus`, `HealthRecord`,
  `DeadLetterEntry`, `CircuitBreakerPolicy`, `CircuitBreakerState`
- **`kernel/events.py`** — `AgentUnhealthy`, `AgentRecovered`, `WorkflowStalled`,
  `DeadLetterAppended`, `DeadLetterRecovered`, `CircuitBreakerTripped`
- **Integration (optional, backward-compatible):**
  - `AgentRuntime(health_monitor=…)` — registers a liveness probe per agent
  - `WorkflowEngine(health_monitor=…, dead_letter=…)` — exhausted steps are
    dead-lettered + emit `WorkflowStalled`
  - `CapabilityExecutor(circuit_breaker=…)` — wraps handler calls in the breaker
- **Tests:** `test_health_monitor.py`, `test_dead_letter.py`,
  `test_circuit_breaker.py`, `test_recovery_engine.py`,
  `test_integration_health.py` (40) — total **377 passed, 3 skipped, 91% cov**

### Honest notes (deferred)
- In-process probes (no external endpoint); in-memory + EventStore dead-letter
- Restart is stop+start (not process-level); workflow recovery dead-letters
  (no checkpoint resume yet); breaker is per-capability
- Single-node only (no distributed health) → ADR-022 Swarm/Teams
- Human escalation is log-only (no alerting channel) → future

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
