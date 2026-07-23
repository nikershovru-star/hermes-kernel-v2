"""plugins/builtin/human_emulation/input_simulator.py — human-like input (B).

Wraps ``pyautogui`` (lazy optional dependency) and adds micro-delays / occasional
typos driven by a ``HumanProfile`` so emulated mouse/keyboard input reads as
human. ``pyautogui.FAILSAFE`` is enabled: moving the cursor to a screen corner
aborts the script (safety net for autonomous input).

AXIS CONTRACT: depends on kernel.domain only.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from kernel.domain import HumanProfile

logger = logging.getLogger("hermes.human.input")


class InputSimulator:
    """Simulate mouse/keyboard input with human-like timing patterns."""

    def __init__(self, profile: HumanProfile) -> None:
        self._profile = profile
        self._pyautogui: Any | None = None

    def _require_pyautogui(self) -> Any:
        """Lazily import pyautogui (optional dep) with a clear error."""
        if self._pyautogui is None:
            try:
                import pyautogui
            except ImportError as exc:
                raise RuntimeError(
                    "InputSimulator requires 'pyautogui'. Install: pip install pyautogui"
                ) from exc
            self._pyautogui = pyautogui
            # Safety: moving the mouse to a screen corner aborts the run.
            self._pyautogui.FAILSAFE = True
        return self._pyautogui

    async def _human_delay(self) -> None:
        """Pause for a random interval drawn from the profile's range."""
        delay = random.uniform(*self._profile.pause_between_actions)
        await asyncio.sleep(delay)

    async def mouse_move(self, x: int, y: int, duration: float | None = None) -> None:
        """Move the cursor to (x, y) with a human-like, distance-based curve."""
        pg = self._require_pyautogui()
        if duration is None:
            cur_x, cur_y = pg.position()
            distance = ((x - cur_x) ** 2 + (y - cur_y) ** 2) ** 0.5
            duration = min(2.0, max(0.2, distance / 500.0))
        await self._human_delay()
        pg.moveTo(x, y, duration=duration, tween=pg.easeInOutQuad)

    async def mouse_click(self, button: str = "left", clicks: int = 1) -> None:
        """Click with a random delay between repeated clicks."""
        pg = self._require_pyautogui()
        await self._human_delay()
        for _ in range(clicks):
            pg.click(button=button)
            if clicks > 1:
                await asyncio.sleep(random.uniform(0.05, 0.2))

    async def key_press(self, key: str) -> None:
        """Press a key after a human-like delay."""
        pg = self._require_pyautogui()
        await self._human_delay()
        pg.press(key)

    async def type_text(self, text: str) -> None:
        """Type ``text`` at the profile's WPM, with rare typos + corrections."""
        pg = self._require_pyautogui()
        chars_per_minute = self._profile.typing_speed_wpm * 5
        delay_per_char = 60.0 / chars_per_minute if chars_per_minute else 0.0
        for char in text:
            await self._human_delay()
            if random.random() < self._profile.typo_rate:
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                pg.typewrite(wrong, interval=delay_per_char)
                await asyncio.sleep(0.1)
                pg.press("backspace")
                await asyncio.sleep(0.05)
            pg.typewrite(char, interval=delay_per_char)
