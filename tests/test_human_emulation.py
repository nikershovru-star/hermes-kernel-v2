"""tests/test_human_emulation.py — Human Emulation builtin plugin (ADR-013).

Playwright / pyautogui are optional and headless-hostile, so every interaction
is mocked. We verify: domain entities, plugin construction + tool registration,
browser agent lazy-import guard + mocked lifecycle, and input simulator timing.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kernel.domain import ActionLog, BrowserSession, HumanProfile
from plugins.builtin.human_emulation.browser_agent import BrowserAgent
from plugins.builtin.human_emulation.human_emulation import HumanEmulationPlugin
from plugins.builtin.human_emulation.input_simulator import InputSimulator
from plugins.builtin.human_emulation.profile_manager import ProfileManager


# --------------------------------------------------------------------------- #
# Domain entities
# --------------------------------------------------------------------------- #
def test_human_profile_defaults() -> None:
    p = HumanProfile(name="alice")
    assert p.typing_speed_wpm == 60
    assert p.typo_rate == 0.02
    assert p.pause_between_actions == (0.5, 2.0)
    assert p.screen_resolution == (1920, 1080)
    assert p.user_agent.startswith("Mozilla")


def test_browser_session_defaults() -> None:
    s = BrowserSession(profile_id="prof-1", url="about:blank")
    assert s.status == "idle"
    assert s.screenshot_path is None
    assert s.last_action is None


def test_action_log_defaults() -> None:
    log = ActionLog(session_id="sess-1", action_type="click", target="#btn")
    assert log.success is True
    assert log.error is None
    assert log.payload == {}


# --------------------------------------------------------------------------- #
# InputSimulator (pyautogui mocked)
# --------------------------------------------------------------------------- #
class TestInputSimulator:
    def test_typing_speed_calculation(self) -> None:
        profile = HumanProfile(name="test", typing_speed_wpm=60, typo_rate=0.0)
        sim = InputSimulator(profile)
        assert sim._profile.typing_speed_wpm == 60

    @pytest.mark.asyncio
    async def test_human_delay_range(self) -> None:
        profile = HumanProfile(name="test", pause_between_actions=(0.1, 0.2))
        sim = InputSimulator(profile)
        await sim._human_delay()  # must not raise

    @pytest.mark.asyncio
    async def test_mouse_move_calls_pyautogui(self) -> None:
        profile = HumanProfile(name="test")
        sim = InputSimulator(profile)
        fake_pg = MagicMock()
        fake_pg.position.return_value = (0, 0)
        fake_pg.easeInOutQuad = "tween"
        with patch.object(sim, "_require_pyautogui", return_value=fake_pg):
            await sim.mouse_move(100, 100)
        fake_pg.moveTo.assert_called_once()
        # (FAILSAFE is armed inside the real _require_pyautogui; the mock patch
        #  bypasses that setter, so we only assert the move call here)

    @pytest.mark.asyncio
    async def test_type_text_calls_typewrite(self) -> None:
        profile = HumanProfile(name="test", typo_rate=0.0)
        sim = InputSimulator(profile)
        fake_pg = MagicMock()
        with patch.object(sim, "_require_pyautogui", return_value=fake_pg):
            await sim.type_text("hi")
        assert fake_pg.typewrite.call_count == 2  # one per char


# --------------------------------------------------------------------------- #
# BrowserAgent (playwright mocked)
# --------------------------------------------------------------------------- #
class TestBrowserAgent:
    @pytest.mark.asyncio
    async def test_start_without_playwright(self) -> None:
        # _async_playwright is None in this env (playwright not installed)
        profile = HumanProfile(name="test")
        agent = BrowserAgent(profile)
        with patch(
            "plugins.builtin.human_emulation.browser_agent._async_playwright", None
        ):
            with pytest.raises(RuntimeError, match="playwright"):
                await agent.start()

    @pytest.mark.asyncio
    async def test_context_manager(self) -> None:
        import sys

        from types import SimpleNamespace

        profile = HumanProfile(name="test")
        # async_playwright() is NOT awaited directly; it returns an object whose
        # .start() is an async method -> use MagicMock for the factory, AsyncMock
        # for the async members.
        mock_pw = MagicMock()  # async_playwright() -> Playwright instance
        mock_pw.return_value.start = AsyncMock(return_value=mock_pw.return_value)
        mock_pw.return_value.stop = AsyncMock()
        mock_browser = AsyncMock()
        mock_context = AsyncMock()
        mock_page = AsyncMock()
        mock_pw.return_value.chromium.launch = AsyncMock(return_value=mock_browser)
        mock_browser.new_context = AsyncMock(return_value=mock_context)
        mock_context.new_page = AsyncMock(return_value=mock_page)
        # patch the lazily-imported optional handle (no real browser needed)
        with patch(
            "plugins.builtin.human_emulation.browser_agent._async_playwright",
            mock_pw,
        ):
            async with BrowserAgent(profile) as agent:
                assert agent._session is not None
                assert agent._session.profile_id == profile.id
                assert agent._page is mock_page


# --------------------------------------------------------------------------- #
# ProfileManager (persistence mocked)
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_profile_manager_crud() -> None:
    persist = MagicMock()
    persist.save = AsyncMock()
    persist.get = AsyncMock(return_value=HumanProfile(name="bob", workspace_id="ws1"))
    persist.list = AsyncMock(return_value=[])
    persist.delete = AsyncMock(return_value=True)
    pm = ProfileManager(persist)
    prof = await pm.create("ws1", name="bob")
    assert prof.name == "bob"
    persist.save.assert_awaited_once()
    fetched = await pm.get("ws1", prof.id)
    assert fetched is not None and fetched.workspace_id == "ws1"


# --------------------------------------------------------------------------- #
# Plugin tool registration (explicit pattern, mirrors desktop_control)
# --------------------------------------------------------------------------- #
def _make_manifest() -> Any:
    from kernel.domain import PluginManifest

    return PluginManifest(
        name="human_emulation",
        version="2.1.0",
        capabilities=["hermes.human.browser", "hermes.human.input"],
        entrypoint="plugins.builtin.human_emulation:HumanEmulationPlugin",
        dependencies=["playwright", "pyautogui"],
    )


def test_plugin_register_tools() -> None:
    from kernel.registry import ToolRegistry

    tr: Any = ToolRegistry()
    plugin = HumanEmulationPlugin(_make_manifest())
    plugin.register_tools(tr)
    expected = {
        "browser_start", "browser_navigate", "browser_click", "browser_type",
        "browser_screenshot", "browser_close", "input_mouse_move", "input_type",
    }
    for name in expected:
        assert tr.get_by_name_sync(name) is not None, f"tool {name} not registered"


def test_plugin_get_capabilities() -> None:
    plugin = HumanEmulationPlugin(_make_manifest())
    assert plugin.get_capabilities() == ["hermes.human.browser", "hermes.human.input"]


@pytest.mark.asyncio
async def test_browser_start_unknown_profile() -> None:
    plugin = HumanEmulationPlugin(_make_manifest())
    result = await plugin.browser_start("nope")
    assert result == {"error": "Profile nope not found"}


# --------------------------------------------------------------------------- #
# Coverage: real method bodies with mocked optional deps
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_input_simulator_click_and_key() -> None:
    profile = HumanProfile(name="test", pause_between_actions=(0.0, 0.0))
    sim = InputSimulator(profile)
    fake_pg = MagicMock()
    with patch.object(sim, "_require_pyautogui", return_value=fake_pg):
        await sim.mouse_click(button="right", clicks=2)
        await sim.key_press("enter")
    fake_pg.click.assert_called_with(button="right")
    fake_pg.press.assert_called_once_with("enter")


@pytest.mark.asyncio
async def test_browser_agent_lifecycle_mocked() -> None:
    profile = HumanProfile(name="test")
    mock_pw = MagicMock()
    mock_pw.return_value.start = AsyncMock(return_value=mock_pw.return_value)
    mock_pw.return_value.stop = AsyncMock()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_pw.return_value.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)

    with patch(
        "plugins.builtin.human_emulation.browser_agent._async_playwright", mock_pw
    ):
        agent = BrowserAgent(profile)
        sess = await agent.start()
        assert sess.status == "idle"
        await agent.navigate("https://example.com")
        assert agent._session.url == "https://example.com"
        assert agent._session.status == "idle"
        await agent.click("#btn")
        await agent.type_text("#q", "hello")
        path = await agent.screenshot()
        assert path.endswith(".png")
        await agent.close()
        assert agent._session.status == "closed"


@pytest.mark.asyncio
async def test_plugin_tools_exercise_mocked() -> None:
    profile = HumanProfile(name="test", pause_between_actions=(0.0, 0.0))
    plugin = HumanEmulationPlugin(_make_manifest())
    plugin.register_profile(profile)

    mock_pw = MagicMock()
    mock_pw.return_value.start = AsyncMock(return_value=mock_pw.return_value)
    mock_pw.return_value.stop = AsyncMock()
    mock_browser = AsyncMock()
    mock_context = AsyncMock()
    mock_page = AsyncMock()
    mock_pw.return_value.chromium.launch = AsyncMock(return_value=mock_browser)
    mock_browser.new_context = AsyncMock(return_value=mock_context)
    mock_context.new_page = AsyncMock(return_value=mock_page)

    fake_pg = MagicMock()
    fake_pg.position.return_value = (0, 0)
    with patch(
        "plugins.builtin.human_emulation.browser_agent._async_playwright", mock_pw
    ), patch.object(InputSimulator, "_require_pyautogui", return_value=fake_pg):
        # browser_start -> creates agent
        res = await plugin.browser_start(profile.id)
        assert res["status"] == "idle"
        # navigate / click / type / screenshot / close
        await plugin.browser_navigate(profile.id, "https://x.com")
        await plugin.browser_click(profile.id, "#a")
        await plugin.browser_type(profile.id, "#a", "text")
        shot = await plugin.browser_screenshot(profile.id)
        assert "screenshot_path" in shot
        await plugin.browser_close(profile.id)
        # input tools
        mv = await plugin.input_mouse_move(profile.id, 10, 20)
        assert mv["action"] == "moved"
        typed = await plugin.input_type(profile.id, "hi")
        assert typed["action"] == "typed"
    # unknown profile path
    err = await plugin.input_type("missing", "x")
    assert err["error"]

@pytest.mark.asyncio
async def test_input_simulator_click_single_and_key() -> None:
    profile = HumanProfile(name="test", pause_between_actions=(0.0, 0.0))
    sim = InputSimulator(profile)
    fake_pg = MagicMock()
    with patch.object(sim, "_require_pyautogui", return_value=fake_pg):
        await sim.mouse_click(button="left", clicks=1)
        await sim.key_press("a")
    fake_pg.click.assert_called_once_with(button="left")
    fake_pg.press.assert_called_once_with("a")


def test_input_simulator_missing_pyautogui() -> None:
    # pyautogui is not installed in this env -> clear error expected
    profile = HumanProfile(name="test")
    sim = InputSimulator(profile)
    with pytest.raises(RuntimeError, match="pyautogui"):
        sim._require_pyautogui()
