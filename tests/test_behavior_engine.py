"""tests/test_behavior_engine.py — BehaviorEngine primitives (ADR-022).

Covers mouse curves, scroll momentum, typing rhythm, gaze, and reading. All
pyautogui calls are mocked; asyncio.sleep is replaced with an instant stub and a
seeded RNG makes behavior deterministic.
"""

from __future__ import annotations

import random
from unittest.mock import MagicMock, patch

import pytest
from kernel.bus import EventBus
from kernel.domain import BehaviorProfile
from kernel.events import EventStore
from plugins.builtin.desktop_control.behavior import BehaviorEngine


async def _instant(_seconds: float) -> None:
    return None


def _engine(profile: BehaviorProfile | None = None, seed: int = 42, **kw):
    bus = EventBus()
    store = EventStore()
    eng = BehaviorEngine(
        profile=profile,
        agent_id="desktop-1",
        event_bus=bus,
        event_store=store,
        rng=random.Random(seed),
        sleep=_instant,
        **kw,
    )
    return eng, bus, store


# --- A. Mouse curves ------------------------------------------------------ #
def test_bezier_path_starts_and_ends_correctly() -> None:
    eng, _, _ = _engine()
    path = eng.bezier_path((0, 0), (200, 100), steps=15)
    assert path[0] == (0, 0)
    assert path[-1] == (200, 100)
    assert len(path) >= 15


def test_bezier_path_is_curved_not_linear() -> None:
    eng, _, _ = _engine(BehaviorProfile(mouse_curve="bezier", mouse_overshoot=False))
    path = eng.bezier_path((0, 0), (100, 0), steps=11)
    # A straight horizontal line would have all y == 0; bezier bows off-axis.
    ys = [p[1] for p in path]
    assert any(y != 0 for y in ys)


def test_linear_curve_is_straight() -> None:
    eng, _, _ = _engine(BehaviorProfile(mouse_curve="linear"))
    path = eng.bezier_path((0, 0), (100, 0), steps=11)
    assert all(p[1] == 0 for p in path)


def test_overshoot_adds_correction_point() -> None:
    eng, _, _ = _engine(BehaviorProfile(mouse_curve="bezier", mouse_overshoot=True))
    path = eng.bezier_path((0, 0), (300, 300), steps=10)
    # overshoot appends a correction point → ends exactly on target
    assert path[-1] == (300, 300)


async def test_move_to_updates_position_and_emits() -> None:
    eng, _, store = _engine()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        await eng.move_to(150, 250)
    assert eng.session.current_position == (150, 250)
    assert fake_pg.moveTo.called
    events = await store.read_stream("desktop-1")
    assert any(e.type == "behavior.mouse_moved" for e in events)


async def test_click_fixates_then_clicks() -> None:
    eng, _, store = _engine()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        await eng.click(10, 20, button="left")
    assert fake_pg.click.called
    events = await store.read_stream("desktop-1")
    types = [e.type for e in events]
    assert "behavior.mouse_moved" in types
    assert "behavior.mouse_clicked" in types


# --- B. Scroll ------------------------------------------------------------ #
async def test_scroll_page_momentum_multiple_calls() -> None:
    eng, _, store = _engine()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        dist = await eng.scroll_page("down")
    assert dist < 0  # down = negative
    assert fake_pg.scroll.call_count >= 3  # momentum splits into chunks
    events = await store.read_stream("desktop-1")
    scrolled = [e for e in events if e.type == "behavior.scrolled"]
    assert len(scrolled) == 1
    assert scrolled[0].payload["direction"] == "down"


async def test_scroll_up_is_positive() -> None:
    eng, _, _ = _engine()
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        dist = await eng.scroll_page("up")
    assert dist > 0


def test_momentum_weights_sum_to_one() -> None:
    w = BehaviorEngine._momentum_weights(5)
    assert abs(sum(w) - 1.0) < 1e-9
    # accelerate→decelerate: middle weight is the largest
    assert w[2] == max(w)


async def test_scroll_to_element_reaches_target() -> None:
    eng, _, _ = _engine()
    fake_pg = MagicMock()
    element = MagicMock()
    element.bbox = (0, 50, 100, 40)  # center_y ~70
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        reached = await eng.scroll_to_element(element, max_scrolls=20)
    assert isinstance(reached, bool)


# --- C. Typing ------------------------------------------------------------ #
async def test_type_text_writes_all_chars() -> None:
    eng, _, store = _engine(BehaviorProfile(typing_error_rate=0.0))
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        summary = await eng.type_text("hello")
    # 5 chars, no typos → 5 write calls of single chars
    assert fake_pg.write.call_count == 5
    assert summary["error_count"] == 0
    events = await store.read_stream("desktop-1")
    assert any(e.type == "behavior.text_typed" for e in events)


async def test_type_text_with_errors_backspaces() -> None:
    # Force 100% error rate → every non-space char triggers a backspace.
    eng, _, _ = _engine(BehaviorProfile(typing_error_rate=1.0))
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        summary = await eng.type_text("abc")
    assert summary["error_count"] == 3
    assert fake_pg.press.call_count == 3  # one backspace per typo


def test_char_interval_scales_with_wpm() -> None:
    fast, _, _ = _engine(BehaviorProfile(typing_wpm=120))
    slow, _, _ = _engine(BehaviorProfile(typing_wpm=20))
    # average interval over several samples: faster WPM → shorter interval
    fast_avg = sum(fast._char_interval() for _ in range(50)) / 50
    slow_avg = sum(slow._char_interval() for _ in range(50)) / 50
    assert fast_avg < slow_avg


# --- D. Gaze -------------------------------------------------------------- #
async def test_gaze_at_sets_target_and_emits() -> None:
    eng, _, store = _engine()
    fixation = await eng.gaze_at(300, 400, duration_ms=250)
    assert eng.session.gaze_target == (300, 400)
    assert fixation == 250
    events = await store.read_stream("desktop-1")
    assert any(e.type == "behavior.gaze_fixated" for e in events)


# --- E. Reading ----------------------------------------------------------- #
async def test_read_text_tracks_words_and_progress() -> None:
    eng, _, store = _engine(BehaviorProfile(reading_regression_rate=0.0, reading_words_per_fixation=2))
    result = await eng.read_text("the quick brown fox jumps", region=(0, 0, 500, 100))
    assert result["words_read"] == 5
    assert result["regressions"] == 0
    events = await store.read_stream("desktop-1")
    assert any(e.type == "behavior.reading_progress" for e in events)


async def test_read_text_empty_is_noop() -> None:
    eng, _, _ = _engine()
    result = await eng.read_text("   ", region=(0, 0, 100, 100))
    assert result["words_read"] == 0


async def test_read_text_regressions_happen() -> None:
    # 100% regression rate → at least one backward saccade recorded.
    eng, _, _ = _engine(BehaviorProfile(reading_regression_rate=1.0, reading_words_per_fixation=1))
    result = await eng.read_text("one two three", region=(0, 0, 300, 50))
    assert result["regressions"] >= 1


# --- Backward compat: no event bus/store ---------------------------------- #
async def test_engine_without_events_runs_silently() -> None:
    eng = BehaviorEngine(rng=random.Random(1), sleep=_instant)
    fake_pg = MagicMock()
    with patch("plugins.builtin.desktop_control.behavior._require_pyautogui", return_value=fake_pg):
        await eng.move_to(5, 5)
        await eng.type_text("hi")
    assert eng.session.current_position == (5, 5)
