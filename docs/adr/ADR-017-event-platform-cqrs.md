# ADR-017 — Event Platform Foundation + Desktop Agent Vision Layer

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review (v2.3.0)
- **Depends on:** ADR-016 (BaseAgent + AgentRuntime + CapabilityExecutor + Artifact),
  ADR-001 (EventBus transport), ADR-007 (workspace isolation)

## Context (the pain)

1. **No Event Platform.** Desktop ops were imperative direct calls — no events,
   no history, no replay, no read models. "What happened on the desktop 5 min
   ago?" was unanswerable.
2. **Desktop is blind.** Screenshots returned raw base64. No OCR, no element
   detection. Human emulation clicked by coordinates, not semantics.
3. **DesktopControlPlugin is not a BaseAgent.** It had sync `load/unload`
   (BasePlugin) but no async lifecycle, so it could not use `AgentRuntime`,
   receive `Task` objects, or return `Artifact`.
4. **CapabilityExecutor had no real handlers.** The kernel never collected tool
   handlers from plugins; desktop capabilities were called directly, not through
   `CapabilityExecutor.execute()`.

This release implements the *foundation* of two v5 platforms: **Event Platform**
(Command/Event Bus + Event Store + CQRS) and **Execution Platform**
(DesktopAgent as a BaseAgent + Vision Layer).

## Decision

### A. Event Platform foundation (kernel/events.py)

**DomainEvent extends the EXISTING `kernel.domain.Event`** (not a duplicate). It
adds `aggregate_id`, `timestamp: datetime`, `version`. Because it IS-A `Event`,
it flows through the existing async `EventBus.publish` unchanged — **no transport
duplication**, axis stays clean. We did NOT create a second `EventBus`; we reused
`kernel.bus.EventBus` and built the store + CQRS layers on top.

* LAYER 3 `EventStore` — append-only journal (in-memory; optional SQLite table).
  No mutation API exists (append-only invariant is structural).
* LAYER 4 `Command`/`CommandBus` — commands are intent; handlers emit events
  via `CommandBus.publish_event` (append + publish in one call).
* LAYER 4 `ReadModel` (ABC) — projections fold events into queryable state.
* LAYER 5 `Query`/`QueryBus` — ask projections.

### B. DesktopAgent (plugins/builtin/desktop_control/desktop_agent.py)

`DesktopAgent(BaseAgent)` — event-driven. `execute(task)` routes `task.capability`
through the injected `CommandBus` → domain handler (pyautogui side-effect via
`asyncio.to_thread`) → `DomainEvent` published + appended → returns `Artifact`
with a provenance chain of event ids. **Every** operation emits an event (no
silent side effects). It DOGFOODs `CapabilityExecutor` (the kernel wires its
capabilities via `CapabilityExecutor.register_agent`). The legacy
`DesktopControlPlugin` is **unchanged** (backward compat; still the MCP surface).

### C. DesktopVision (plugins/builtin/desktop_control/vision.py)

`DesktopVision` — OCR (`pytesseract`) + element detection (OCR-driven bounding
boxes) + fuzzy `find_element`. Pure CV, depends only on `kernel.domain`. All
heavy deps lazy-imported; missing dep → clear `RuntimeError`. For v2.3.0 the
detection path is OCR-based (text regions as clickable) — documented honestly.

### D. Capability wiring (kernel/capability.py)

`CapabilityExecutor.register_agent(agent)` wires a BaseAgent's capabilities as
handlers that build a `Task` from params/context and delegate to
`agent.execute`. Manual wiring for v2.3.0 (auto-discovery is ADR-018).

### E. Persistence (EventStore)

In-memory + optional SQLite append-only table `events(id, aggregate_id,
event_type, payload_json, timestamp, version)`. Read models are in-memory
projections, rebuilt from the stream on startup (replay). No snapshots (ADR-019).

## Architecture / axis

```
kernel.events          → [kernel.domain, kernel.bus]   (reuses EventBus)
kernel.agent           → [kernel.domain, kernel.events] (AgentRuntime publishes)
kernel.capability      → [kernel.domain, kernel.events, kernel.agent] (register_agent)
plugins.builtin.desktop_control        → [kernel, kernel.domain, kernel.events, plugins]
plugins.builtin.desktop_control.vision → [kernel.domain]  (pure CV)
```

tach: explicit submodules `plugins.builtin.desktop_control.vision` +
`plugins.builtin.desktop_control` extended with `kernel.events`.

## Consequences

- +19 tests across 4 new files (event platform, desktop agent, vision, CQRS).
- `DesktopAgent` now usable via `AgentRuntime` + `CapabilityExecutor`; emits
  events for every op.
- `DesktopVision` makes the desktop "see" (OCR + element detection).
- Event Store append-only; read models give replayable history.

## Honest notes (deferred — for next ADRs)

- **Did NOT duplicate EventBus.** Reused `kernel.bus.EventBus`; `DomainEvent`
  extends `Event`. If a richer async bus is ever needed, extend — don't fork.
- **Auto-discovery of handlers is future (ADR-018).** v2.3.0 wires manually.
- **Snapshots are future (ADR-019).** Read models rebuild from full stream.
- **Event Store is in-memory + SQLite; distributed journal is future.**
- **DesktopVision is OCR-based.** Full YOLOv8-nano integration is future work.
- **`DesktopControlPlugin` (legacy BasePlugin) left intact** — dual surface
  (MCP tools + event-driven agent) during transition.
