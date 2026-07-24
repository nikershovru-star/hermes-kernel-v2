# ADR-022 — Behavior Engine (Human Emulation 2.0: Scroll, Mouse Trails, Typing, Reading)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.8.0)
- **Depends on:** ADR-013 (Human Emulation), ADR-017 (Agent/Plugin Unification + Event Platform), ADR-021 (Health & Recovery)

---

## Context

The v5 Capability Platform lists Behavior Engine as the layer that makes desktop
automation look human. Until now `DesktopAgent` drove `pyautogui` directly:
instant mouse teleports, linear scrolls, uniform 10 ms typing, and clicks with
no gaze fixation. Four pains motivated this release:

1. **Robotic automation** — instant teleport clicks (0 ms) are trivially
   flagged by anti-bot detection.
2. **No scroll behavior** — `pyautogui.scroll(-500)` jumps instantly; no
   momentum, deceleration, or reading pauses.
3. **No typing rhythm** — uniform intervals with no bursts, typos, or
   corrections.
4. **No reading/gaze simulation** — clicks fire without "looking" first; no
   fixation → saccade pattern.

## Decision

Introduce `plugins/builtin/desktop_control/behavior.py` (`BehaviorEngine`) plus
supporting domain/events/persistence, all under the existing
`plugins.builtin.desktop_control` tach umbrella (no new module):

- **`BehaviorProfile` / `BehaviorSession`** (`kernel/domain.py`) — tuning knobs
  (mouse speed/curve/overshoot, scroll momentum/distance/pauses, typing WPM/error
  rate/bursts, gaze fixation/saccade, reading words-per-fixation/regression) and
  mutable session state (current position, scroll position, gaze target, action
  log).
- **`BehaviorEngine`** — async primitives: `move_to` (quadratic Bezier path with
  overshoot + correction), `click` (move → gaze fixation → click → settle),
  `scroll_page` (accelerate→coast→decelerate momentum with reading pause),
  `scroll_to_element`, `type_text` (WPM-derived variable intervals, bursts, typo
  + backspace + retype), `gaze_at` (saccade + fixation), `read_text` (word-group
  fixations with bounded regressions). Every `pyautogui` call is dispatched via
  `asyncio.to_thread`; timing uses an injectable `sleep` and randomness an
  injectable `random.Random` for deterministic fast tests. Event emission
  (`event_bus`/`event_store`) is optional.
- **6 events** (`kernel/events.py`) — `MouseMoved`, `MouseClicked`, `Scrolled`,
  `TextTyped`, `GazeFixated`, `ReadingProgress`.
- **`HumanBehaviorProfile` + `HumanProfileStore`** — CRUD + optional SQLite
  persistence for named behavior profiles. (Named `HumanBehaviorProfile` to
  avoid colliding with the pre-existing ADR-013 `HumanProfile`.)
- **Integration (optional, backward-compatible):** `DesktopAgent(behavior=…)`
  routes `desktop.click/type/scroll/read` through the engine when present and
  falls back to the legacy `CommandBus` path when `behavior=None`. `DesktopVision`
  gains `UIElement.center*` properties + `find_element_for_behavior` for
  gaze/mouse targeting.

## Consequences

- **+35 tests** (`test_behavior_engine.py` 18, `test_human_profile.py` 10,
  `test_behavior_integration.py` 7) — total **412 passed, 3 skipped**; kernel
  coverage **91%**, `behavior.py` 95%, `human_profile.py` 100%.
- **No new runtime dependency** — pure math + `asyncio` + existing `pyautogui`.
- tach green (behavior/human_profile under the desktop_control umbrella).

### Honest notes (deferred)

- Bezier curves are **approximated** (quadratic through one perpendicular
  control point, not true cubic) for performance.
- Typing/timing randomness uses **uniform** distributions, not Gaussian —
  sufficient for anti-bot timing variance, not a true keystroke-dynamics model.
- Gaze simulation is **2D only** — no head movement, no blink simulation.
- Reading uses a **simple word split** (no NLP for sentence structure); `read_text`
  regressions re-count revisited groups (fixations, not unique words).
- **No anti-detection randomization beyond timing** — no viewport jitter, no UA
  rotation (those live in the ADR-013 human_emulation layer).
- Profile persistence is **in-memory + optional SQLite** — no cloud sync.
- `scroll_to_element` is **best-effort** without a live viewport (bounded scroll
  toward the element's Y).
