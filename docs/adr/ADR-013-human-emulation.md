# ADR-013 — Human Emulation Layer

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review
- **Depends on:** ADR-007 (workspace isolation), ADR-011 (desktop control)

## Context

The user requested that Hermes can emulate human actions (browse sites, click,
type) autonomously — e.g. when the user is away. This is broader than remote
desktop control (ADR-011): it is *agent behaviour* with human-like timing
patterns, driven by a reusable "digital twin" profile.

## Decision

Create `plugins/builtin/human_emulation/` — a builtin plugin (v2.1.0) bundling:

1. **`HumanProfile`** (domain entity) — digital-twin settings: `typing_speed_wpm`,
   `typo_rate`, `pause_between_actions`, `preferred_browser`, `screen_resolution`,
   `user_agent`. Workspace-isolated (ADR-007).
2. **`BrowserSession`** (domain entity) — one browser tab/window; `profile_id` FK,
   `url`, `status`, `screenshot_path`, `last_action`.
3. **`ActionLog`** (domain entity) — audit trail of every emulated action
   (`session_id`, `action_type`, `target`, `payload`, `success`, `error`).
4. **`BrowserAgent`** (`browser_agent.py`) — async Playwright wrapper (navigate,
   click, type with human speed + rare typos, screenshot, context manager).
5. **`InputSimulator`** (`input_simulator.py`) — pyautogui wrapper with human-like
   micro-delays and occasional typos; `FAILSAFE = True` safety abort.
6. **`ProfileManager`** (`profile_manager.py`) — async CRUD over `PersistenceRegistry`.
7. **`HumanEmulationPlugin`** (`human_emulation.py`) — `BasePlugin`; 8 tools
   (`browser_start/navigate/click/type/screenshot/close`, `input_mouse_move/type`)
   declared with `@sdk.tool` and registered explicitly via `register_tools()`
   (no global SDK state at import — same pattern as `desktop_control`).

## Architecture / axis

```
plugins.builtin.human_emulation  →  [kernel, kernel.domain, plugins]
├── browser_agent.py      → playwright (LAZY import, optional dep)
├── input_simulator.py    → pyautogui (LAZY import, optional dep)
├── human_emulation.py    → Plugin class + explicit tool registration
├── profile_manager.py    → HumanProfile CRUD (workspace-scoped)
└── (future) cv_module.py / behavior_engine.py — OCR + richer "humanity"
```

Declared as an **explicit tach submodule** (submodules are not transitively
inherited by tach; depends on `plugins`, not `plugins.sdk`).

## Security / safety

- Profiles + sessions + action logs are workspace-isolated (ADR-007).
- `ActionLog` provides a full audit trail of every emulated action.
- No secrets in `HumanProfile` (use the browser's own credential store).
- `pyautogui.FAILSAFE = True`: moving the cursor to a screen corner aborts.
- Playwright launches **visible** (headless=False) so behaviour is observable.

## Consequences

- New optional deps behind the `[human]` extra (`playwright`, `pyautogui`) — the
  kernel imports fine without them (lazy import + clear `RuntimeError`).
- 3 new domain entities registered in `PersistenceRegistry._TYPE_TO_CLASS`.
- 8 new Tools, registered explicitly (idempotent) after `load()`.
- Tests mock Playwright/pyautogui — no real browser/desktop in CI.

## Future work

- `cv_module.py` — OCR + element detection from screenshots.
- `behavior_engine.py` — richer "humanity" (scroll patterns, reading pauses).
- RustDesk integration for remote browser on another machine.
