"""plugins/builtin/desktop_control/behavior.py — Behavior Engine (ADR-022).

Human-like behavior primitives for desktop automation: Bezier mouse curves,
scroll momentum, typing rhythm, gaze fixation, and reading simulation. Wraps
``pyautogui`` (lazy optional dep) with curves, rhythms, and pauses so the
automation does not look robotic to anti-bot detection.

All public methods are ``async`` (non-blocking for the event loop). Every
``pyautogui`` call is dispatched via ``asyncio.to_thread`` because pyautogui is
synchronous. Timing uses ``asyncio.sleep`` (injectable for fast tests) and
randomness uses an injectable ``random.Random`` (seedable for determinism).

Event emission (MouseMoved / MouseClicked / Scrolled / TextTyped / GazeFixated
/ ReadingProgress) is OPTIONAL — when ``event_bus``/``event_store`` are None the
engine runs silently (keeps it usable standalone and in tests).

AXIS CONTRACT: depends on kernel.domain (BehaviorProfile / BehaviorSession) and
kernel.events (behavior events + EventBus/EventStore). Never imports kernel
internals beyond those, never imports mcp/ or other builtins.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Awaitable, Callable

from kernel.domain import BehaviorProfile, BehaviorSession
from kernel.events import (
    EventBus,
    EventStore,
    GazeFixated,
    MouseClicked,
    MouseMoved,
    ReadingProgress,
    Scrolled,
    TextTyped,
)

logger = logging.getLogger("hermes.desktop.behavior")


def _require_pyautogui():
    try:
        import pyautogui  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - host install dependent
        raise RuntimeError(
            "BehaviorEngine requires pyautogui; install the 'desktop' extra:\n"
            "  pip install 'hermes-kernel-v2[desktop]'"
        ) from exc
    return pyautogui


SleepFn = Callable[[float], Awaitable[None]]


class BehaviorEngine:
    """Human-like behavior primitives for desktop automation (ADR-022)."""

    def __init__(
        self,
        profile: BehaviorProfile | None = None,
        *,
        agent_id: str = "desktop",
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        rng: random.Random | None = None,
        sleep: SleepFn | None = None,
        curve_steps: int = 20,
    ) -> None:
        self.profile = profile or BehaviorProfile()
        self.session = BehaviorSession(profile=self.profile)
        self._agent_id = agent_id
        self._bus = event_bus
        self._store = event_store
        self._rng = rng or random.Random()
        self._sleep: SleepFn = sleep or asyncio.sleep
        self._curve_steps = max(2, curve_steps)

    # -- helpers ---------------------------------------------------------- #
    def _rand_ms(self, span: tuple[int, int]) -> int:
        lo, hi = span
        if hi <= lo:
            return lo
        return self._rng.randint(lo, hi)

    async def _pause_ms(self, span: tuple[int, int]) -> int:
        ms = self._rand_ms(span)
        await self._sleep(ms / 1000.0)
        return ms

    async def _emit(self, event: Any) -> None:
        if self._store is not None:
            await self._store.append(event)
        if self._bus is not None:
            self._bus.publish(event)
        self.session.action_log.append(getattr(event, "id", ""))

    # -- curve math ------------------------------------------------------- #
    def bezier_path(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        steps: int | None = None,
    ) -> list[tuple[int, int]]:
        """Return a list of points along the mouse path (curve per profile).

        ``bezier`` / ``catmull`` use a quadratic Bezier through a perpendicular
        control point (with optional overshoot); ``linear`` interpolates.
        Approximated (quadratic, not cubic) for performance — see ADR-022.
        """
        steps = steps or self._curve_steps
        sx, sy = start
        ex, ey = end
        curve = self.profile.mouse_curve

        if curve == "linear":
            return [
                (round(sx + (ex - sx) * t), round(sy + (ey - sy) * t))
                for t in (i / (steps - 1) for i in range(steps))
            ]

        # Control point: midpoint offset perpendicular to the travel vector.
        mx, my = (sx + ex) / 2.0, (sy + ey) / 2.0
        dx, dy = ex - sx, ey - sy
        dist = max(1.0, (dx * dx + dy * dy) ** 0.5)
        # perpendicular unit vector
        px, py = -dy / dist, dx / dist
        # bow magnitude scales with distance and a little jitter
        bow = dist * 0.15 * self._rng.uniform(0.5, 1.5)
        cx, cy = mx + px * bow, my + py * bow

        points: list[tuple[int, int]] = []
        for i in range(steps):
            t = i / (steps - 1)
            # quadratic Bezier B(t) = (1-t)^2 P0 + 2(1-t)t C + t^2 P1
            omt = 1 - t
            bx = omt * omt * sx + 2 * omt * t * cx + t * t * ex
            by = omt * omt * sy + 2 * omt * t * cy + t * t * ey
            points.append((round(bx), round(by)))

        # Overshoot: nudge the final point past the target, then correct back.
        if self.profile.mouse_overshoot and dist > 20:
            over = 1.0 + 0.04 * self._rng.uniform(0.5, 1.5)
            ox = round(sx + (ex - sx) * over)
            oy = round(sy + (ey - sy) * over)
            points[-1] = (ox, oy)
            points.append((ex, ey))  # correction back to target
        else:
            points[-1] = (ex, ey)
        return points

    # -- Mouse ------------------------------------------------------------ #
    async def move_to(self, x: int, y: int, duration: float | None = None) -> None:
        """Move mouse to (x, y) along a curve with speed variation."""
        pyautogui = _require_pyautogui()
        start = self.session.current_position
        path = self.bezier_path(start, (x, y))
        # total duration scales inversely with mouse_speed
        base = duration if duration is not None else 0.4 * (len(path) / self._curve_steps)
        total = max(0.0, base / max(0.1, self.profile.mouse_speed))
        per_step = total / max(1, len(path))
        t0 = time.monotonic()
        for (px, py) in path:
            await asyncio.to_thread(pyautogui.moveTo, px, py)
            if per_step > 0:
                await self._sleep(per_step)
        duration_ms = (time.monotonic() - t0) * 1000.0
        self.session.current_position = (x, y)
        await self._emit(
            MouseMoved(self._agent_id, start, (x, y), duration_ms, self.profile.mouse_curve)
        )

    async def click(self, x: int, y: int, button: str = "left") -> None:
        """Move to (x, y), fixate the gaze, then click, then settle."""
        pyautogui = _require_pyautogui()
        await self.move_to(x, y)
        fixation = await self._pause_ms(self.profile.gaze_fixation_ms)
        await asyncio.to_thread(pyautogui.click, x=x, y=y, button=button)
        await self._emit(MouseClicked(self._agent_id, (x, y), fixation, button))
        await self._pause_ms(self.profile.mouse_pause_ms)

    # -- Scroll ----------------------------------------------------------- #
    async def scroll_page(self, direction: str = "down") -> int:
        """Scroll with momentum: accelerate → coast → decelerate.

        Emits a single ``Scrolled`` event summarizing the burst. Returns the
        total distance scrolled in pixels (signed by direction).
        """
        pyautogui = _require_pyautogui()
        total_distance = self._rand_ms(self.profile.scroll_distance_px)
        sign = -1 if direction == "down" else 1
        # Momentum: split into 3..6 chunks, accelerate then decelerate.
        chunks = self._rng.randint(3, 6)
        weights = self._momentum_weights(chunks)
        pauses: list[int] = []
        for w in weights:
            step = max(1, round(total_distance * w))
            await asyncio.to_thread(pyautogui.scroll, sign * step)
            if self.profile.scroll_momentum:
                pauses.append(await self._pause_ms(self.profile.scroll_pause_ms))
        # reading pause at the end
        pauses.append(await self._pause_ms(self.profile.scroll_reading_pause_ms))
        self.session.scroll_position += sign * total_distance
        await self._emit(
            Scrolled(self._agent_id, direction, total_distance, pauses)
        )
        return sign * total_distance

    @staticmethod
    def _momentum_weights(chunks: int) -> list[float]:
        """Return normalized weights forming an accelerate→decelerate profile."""
        # triangular window: small, big, ..., small
        half = (chunks - 1) / 2.0
        raw = [1.0 - abs(i - half) / (half + 1) for i in range(chunks)]
        s = sum(raw) or 1.0
        return [r / s for r in raw]

    async def scroll_to_element(self, element: Any, max_scrolls: int = 10) -> bool:
        """Scroll until ``element`` (with ``.bbox``) is near viewport center.

        Best-effort: without a live viewport we scroll a bounded number of
        times toward the element's Y. Returns True if considered reached.
        """
        target_y = 0
        bbox = getattr(element, "bbox", None)
        if bbox:
            target_y = bbox[1] + bbox[3] // 2
        for _ in range(max_scrolls):
            if abs(self.session.scroll_position - target_y) < 100:
                return True
            direction = "down" if target_y > self.session.scroll_position else "up"
            await self.scroll_page(direction)
        return abs(self.session.scroll_position - target_y) < 200

    # -- Typing ----------------------------------------------------------- #
    def _char_interval(self) -> float:
        """Seconds per character from the WPM target (5 chars/word avg)."""
        wpm = max(1, self.profile.typing_wpm)
        cps = (wpm * 5) / 60.0
        base = 1.0 / cps
        return base * self._rng.uniform(0.6, 1.4)  # variable rhythm

    async def type_text(self, text: str) -> dict[str, Any]:
        """Type ``text`` with human rhythm: bursts, variable speed, typos.

        Returns a summary dict {"wpm", "error_count", "duration_ms"}.
        """
        pyautogui = _require_pyautogui()
        t0 = time.monotonic()
        error_count = 0
        burst_remaining = self._rand_ms(self.profile.typing_burst_size)
        for ch in text:
            # Occasional typo: type a wrong char, then backspace, then correct.
            if ch != " " and self._rng.random() < self.profile.typing_error_rate:
                wrong = self._rng.choice("abcdefghijklmnopqrstuvwxyz")
                await asyncio.to_thread(pyautogui.write, wrong)
                await self._sleep(self._char_interval())
                await asyncio.to_thread(pyautogui.press, "backspace")
                await self._sleep(self._char_interval())
                error_count += 1
            await asyncio.to_thread(pyautogui.write, ch)
            await self._sleep(self._char_interval())
            burst_remaining -= 1
            if burst_remaining <= 0:
                await self._pause_ms(self.profile.mouse_pause_ms)
                burst_remaining = self._rand_ms(self.profile.typing_burst_size)
        duration_ms = (time.monotonic() - t0) * 1000.0
        await self._emit(
            TextTyped(self._agent_id, text, self.profile.typing_wpm, error_count, duration_ms)
        )
        return {"wpm": self.profile.typing_wpm, "error_count": error_count, "duration_ms": duration_ms}

    # -- Gaze / Reading --------------------------------------------------- #
    async def gaze_at(self, x: int, y: int, duration_ms: int | None = None) -> int:
        """Move the "gaze" to (x, y) with a saccade, fixate for a duration."""
        await self._pause_ms(self.profile.gaze_saccade_ms)  # saccade
        fixation = duration_ms if duration_ms is not None else self._rand_ms(self.profile.gaze_fixation_ms)
        await self._sleep(fixation / 1000.0)
        self.session.gaze_target = (x, y)
        await self._emit(GazeFixated(self._agent_id, (x, y), fixation))
        return fixation

    async def read_text(self, text: str, region: tuple[int, int, int, int]) -> dict[str, Any]:
        """Simulate reading ``text`` in ``region``: saccades + fixations.

        Splits into words, groups by ``reading_words_per_fixation``, fixates on
        each group's approximate position, applies random regressions. Emits a
        ``ReadingProgress`` summary. Returns {"words_read", "regressions"}.
        """
        words = text.split()
        if not words:
            return {"words_read": 0, "regressions": 0, "duration_ms": 0.0}
        rx, ry, rw, rh = region
        per_fix = max(1, self.profile.reading_words_per_fixation)
        groups = [words[i : i + per_fix] for i in range(0, len(words), per_fix)]
        t0 = time.monotonic()
        regressions = 0
        max_regressions = len(groups)  # hard bound → no infinite loop
        words_read = 0
        i = 0
        just_regressed = False
        while i < len(groups):
            # position: spread groups left→right across the region width
            frac = i / max(1, len(groups) - 1)
            gx = rx + round(rw * frac)
            gy = ry + rh // 2
            await self.gaze_at(gx, gy, self._rand_ms(self.profile.gaze_fixation_ms))
            words_read += len(groups[i])
            # random backward saccade (regression) — never two in a row and
            # globally bounded so the scan always terminates (no infinite loop).
            if (
                i > 0
                and not just_regressed
                and regressions < max_regressions
                and self._rng.random() < self.profile.reading_regression_rate
            ):
                regressions += 1
                just_regressed = True
                i -= 1
                continue
            just_regressed = False
            i += 1
        duration_ms = (time.monotonic() - t0) * 1000.0
        await self._emit(
            ReadingProgress(self._agent_id, words_read, regressions, duration_ms)
        )
        return {"words_read": words_read, "regressions": regressions, "duration_ms": duration_ms}


__all__ = ["BehaviorEngine"]
