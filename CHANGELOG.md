# Changelog

All notable changes to Hermes Kernel v2 are documented here. The format is
loosely based on [Keep a Changelog](https://keepachangelog.com/); this project
adheres to **semantic versioning** (MAJOR.MINOR.PATCH).

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
[v2.0.0]: #v200--2026-07-23--polish-release
[v1.2.0]: #v120--2026-07-23--mcp-streamable-http-hardening
[v1.1.0]: #v110--2026-07-23--desktop-control-plugin
[v1.0.0]: #v100--2026-07-23--core-kernel-stable
