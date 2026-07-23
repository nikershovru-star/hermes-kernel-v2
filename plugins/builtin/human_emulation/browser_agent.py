"""plugins/builtin/human_emulation/browser_agent.py — async Playwright wrapper (A).

Drives a real (visible) browser so emulated behaviour looks human. Playwright is
a **lazy optional dependency**: imported only inside ``start()`` so the kernel
imports this module without the browser stack installed.

AXIS CONTRACT: depends on kernel (domain, persistence). Never imports plugins.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from kernel.domain import BrowserSession, HumanProfile
from kernel.persistence import PersistenceRegistry

logger = logging.getLogger("hermes.human.browser")

# Playwright is an OPTIONAL dependency. Import the module lazily at module load
# so this file imports cleanly without the browser stack; if absent, we set the
# handle to None and fail fast with a clear error on start().
try:  # pragma: no cover - exercised only when playwright is installed
    from playwright.async_api import async_playwright as _async_playwright
except (ImportError, ModuleNotFoundError):  # optional dep not installed
    _async_playwright = None  # type: ignore[assignment]


class BrowserAgent:
    """Async browser agent using Playwright (human-emulation oriented)."""

    def __init__(
        self,
        profile: HumanProfile,
        persistence: PersistenceRegistry | None = None,
    ) -> None:
        self._profile = profile
        self._persistence = persistence
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._session: BrowserSession | None = None

    async def start(self) -> BrowserSession:
        """Launch the browser configured from the human profile."""
        if _async_playwright is None:  # optional dep not installed
            raise RuntimeError(
                "BrowserAgent requires 'playwright'. Install: "
                "pip install playwright && playwright install chromium"
            )
        width, height = self._profile.screen_resolution
        self._playwright = await _async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=False,  # visible browser for human-like behaviour
            args=[f"--window-size={width},{height}"],
        )
        self._context = await self._browser.new_context(
            viewport={"width": width, "height": height},
            user_agent=self._profile.user_agent,
        )
        self._page = await self._context.new_page()

        self._session = BrowserSession(
            profile_id=self._profile.id,
            url="about:blank",
            status="idle",
        )
        if self._persistence is not None:
            await self._persistence.save(self._session)
        return self._session

    async def navigate(self, url: str) -> None:
        """Navigate to ``url`` (network-idle wait) and persist session state."""
        if self._page is None or self._session is None:
            raise RuntimeError("Browser not started")
        self._session.status = "loading"
        await self._page.goto(url, wait_until="networkidle")
        self._session.url = url
        self._session.status = "idle"
        if self._persistence is not None:
            await self._persistence.save(self._session)

    async def click(self, selector: str) -> None:
        """Click an element after a human-like random pause."""
        if self._page is None:
            raise RuntimeError("Browser not started")
        delay = random.uniform(*self._profile.pause_between_actions)
        await asyncio.sleep(delay)
        await self._page.click(selector)

    async def type_text(self, selector: str, text: str) -> None:
        """Type ``text`` into ``selector`` at human speed, with rare typos."""
        if self._page is None:
            raise RuntimeError("Browser not started")
        chars_per_minute = self._profile.typing_speed_wpm * 5
        delay_per_char = 60.0 / chars_per_minute if chars_per_minute else 0.0
        for char in text:
            if random.random() < self._profile.typo_rate:
                wrong = random.choice("abcdefghijklmnopqrstuvwxyz")
                await self._page.type(selector, wrong, delay=delay_per_char * 1000)
                await asyncio.sleep(0.1)
                await self._page.press(selector, "Backspace")
                await asyncio.sleep(0.05)
            await self._page.type(selector, char, delay=delay_per_char * 1000)

    async def screenshot(self, path: str | None = None) -> str:
        """Capture a PNG screenshot; return its saved path."""
        if self._page is None or self._session is None:
            raise RuntimeError("Browser not started")
        screenshot_path = path or f"/tmp/hermes_screenshot_{self._session.id}.png"
        await self._page.screenshot(path=screenshot_path)
        self._session.screenshot_path = screenshot_path
        if self._persistence is not None:
            await self._persistence.save(self._session)
        return screenshot_path

    async def close(self) -> None:
        """Close browser + playwright and mark the session closed."""
        if self._browser is not None:
            await self._browser.close()
        if self._playwright is not None:
            await self._playwright.stop()
        if self._session is not None:
            self._session.status = "closed"
            if self._persistence is not None:
                await self._persistence.save(self._session)

    async def __aenter__(self) -> "BrowserAgent":
        await self.start()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()
