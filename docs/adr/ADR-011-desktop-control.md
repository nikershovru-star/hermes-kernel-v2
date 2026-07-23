# ADR-011: Desktop Control Plugin

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review
- **Supersedes / Related:** ADR-001 (Clean Architecture), ADR-010 (Plugin CLI UX)

## Context

Operators need to drive the host desktop (mouse, keyboard, screenshots) as
first-class kernel capabilities — e.g. for agent-assisted automation, QA
recording, or remote operator assistance. These are global, OS-level side
effects (not workspace-scoped data), so they are exposed as a **builtin plugin**
under the `hermes.desktop` capability namespace, registered through the normal
plugin + SDK machinery.

## Decision

Ship `plugins/builtin/desktop_control/` as a builtin plugin:

1. **Axis:** `plugins → kernel` only. The plugin depends on `kernel.domain`
   (`PluginManifest`, `Tool`), `kernel.registry` (`ToolRegistry`), and the SDK
   (`@sdk.agent` / `@sdk.tool`). It does **not** import from `mcp` or cause a
   `kernel → plugins` inversion. `tach check` stays green.
2. **Decorator style:** `DesktopControlPlugin(BasePlugin)` declares its tool
   surface with `@sdk.tool` on each method (metadata harvesting, no import-time
   side effects). Because `@sdk.agent` registers on construction and requires
   `configure_sdk(...)` to have run *before the module is imported* (fragile for
   builtins loaded by `auto_load`), we use **explicit registration** instead:
   the kernel calls `plugin.register_tools(tool_registry)` and
   `plugin.register_agent(agent_registry)` after `load()`. Idempotent, axis-clean,
   and import-safe. Tool/agent entities land in the same `ToolRegistry` /
   `AgentRegistry` as any SDK agent.
3. **Lazy optional deps:** `pyautogui` and `Pillow` are imported **inside**
   `load()` / per tool call, never at module top. If absent, the plugin loads
   but tool calls raise a clear `RuntimeError`. Declared via
   `[project.optional-dependencies] desktop = ["pyautogui>=0.9", "Pillow>=10.0"]`.
4. **Async surface:** every tool is `async` and runs the blocking `pyautogui`
   call via `asyncio.to_thread`, so a desktop action never blocks the kernel
   event loop.
5. **Platform guard:** `load()` raises `RuntimeError` if
   `platform.system()` is not Windows/Linux/Darwin (pyautogui needs a display
   server). Screenshots additionally fail clearly if no DISPLAY on Linux.
6. **Workspace:** desktop control is a global singleton (no workspace_id
   scoping). The plugin is still workspace-aware at registration time (it
   carries a `PluginManifest` with `workspace_id` semantics like any plugin),
   but tool effects are host-global by design.

### Tools

| Tool | Params | Returns | Notes |
|------|--------|---------|-------|
| `mouse_move` | `x: int, y: int` | `{"ok": bool}` | `pyautogui.moveTo` |
| `mouse_click` | `button: str="left", clicks: int=1` | `{"ok": bool}` | `pyautogui.click` |
| `key_press` | `key: str` | `{"ok": bool}` | `pyautogui.press` |
| `type_text` | `text: str, interval: float=0.01` | `{"ok": bool}` | `pyautogui.write` |
| `screenshot` | `region: list[int] | None = None` | `{"image": str}` | base64 PNG via Pillow |

## Consequences

- **Good:** desktop automation is a first-class, testable, axis-clean capability;
  optional deps kept out of the core install; async-friendly; CI green.
- **Bad / cost:** pyautogui is headless-hostile — tests **mock** pyautogui (no
  real mouse moves in CI); real desktop use requires a display + the extra
  `desktop` extra.
- **Platform limit (honest):** on Windows CI / headless Linux, pyautogui cannot
  actually move a cursor; we verify behaviour via mocks, not real input.

## Rejected Alternatives

- **Put it in `kernel/`:** would violate the axis (`kernel → plugins` inversion)
  and pull an OS-GUI dependency into the core. Plugins are the extension point.
- **RustDesk native client:** overkill for v1.1.0; pyautogui covers mouse/key/
  screenshot locally. RustDesk remains a future remote-control option.
