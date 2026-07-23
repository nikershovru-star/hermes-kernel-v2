"""tests/test_desktop_control.py — Desktop Control builtin plugin (ADR-011).

pyautogui/Pillow are optional and headless-hostile, so every interaction is
mocked. We verify: plugin construction + load, tool registration into
ToolRegistry, each tool's behaviour, and the platform guard.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from kernel.domain import PluginManifest
from kernel.registry import ToolRegistry
from plugins.builtin.desktop_control import DesktopControlPlugin
from plugins.sdk import configure_sdk
from kernel.bus import EventBus
from kernel.capability import CapabilityRegistry
from kernel.registry import AgentRegistry


def _make_manifest() -> PluginManifest:
    return PluginManifest(
        plugin_id="desktop_control",
        name="desktop_control",
        version="0.1.0",
        capabilities=[
            "hermes.desktop.mouse_move",
            "hermes.desktop.mouse_click",
            "hermes.desktop.key_press",
            "hermes.desktop.type_text",
            "hermes.desktop.screenshot",
        ],
        entrypoint="plugins.builtin.desktop_control:DesktopControlPlugin",
        dependencies=[],
    )


def _mock_pyautogui() -> MagicMock:
    """A fake pyautogui whose screenshot returns a fake PIL image buffer."""
    pg = MagicMock()
    fake_img = MagicMock()
    # simulate PIL Image with save() writing PNG bytes
    buf = b"\x89PNG\r\n\x1a\n fake-png-bytes"
    fake_img.save.side_effect = lambda f, **kw: f.write(buf)
    pg.screenshot.return_value = fake_img
    return pg


@pytest.fixture
def registries():
    """Configure the SDK so @sdk.agent auto-registers into real registries."""
    ar = AgentRegistry()
    tr = ToolRegistry()
    cr = CapabilityRegistry(tr)
    bus = EventBus()
    configure_sdk(agent_registry=ar, tool_registry=tr, capability_registry=cr, bus=bus)
    return ar, tr, cr, bus


def test_plugin_construct_and_load(registries) -> None:
    manifest = _make_manifest()
    plugin = DesktopControlPlugin(manifest)
    assert plugin.name == "desktop_control"
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=_mock_pyautogui(),
    ), patch(
        "plugins.builtin.desktop_control.desktop_control._require_pillow",
        return_value=MagicMock(),
    ), patch("platform.system", return_value="Windows"):
        assert plugin.load() is True
    assert plugin.get_capabilities() == [
        "hermes.desktop.mouse_move",
        "hermes.desktop.mouse_click",
        "hermes.desktop.key_press",
        "hermes.desktop.type_text",
        "hermes.desktop.screenshot",
    ]


def test_tools_registered_via_sdk() -> None:
    tr = ToolRegistry()
    ar = AgentRegistry()
    plugin = DesktopControlPlugin(_make_manifest())
    plugin.register_agent(ar)
    plugin.register_tools(tr)
    # tools are registered into the (real) ToolRegistry
    for name in (
        "mouse_move",
        "mouse_click",
        "key_press",
        "type_text",
        "screenshot",
    ):
        assert tr.get_by_name_sync(name) is not None, f"tool {name} not registered"
    assert ar.get_by_name("desktop_control") is not None


def test_register_tools_idempotent(registries) -> None:
    _ar, tr, _cr, _bus = registries
    plugin = DesktopControlPlugin(_make_manifest())
    plugin.register_tools(tr)
    plugin.register_tools(tr)  # second call must not raise
    # all 5 desktop tools resolvable by name after a repeat registration
    for name in (
        "mouse_move",
        "mouse_click",
        "key_press",
        "type_text",
        "screenshot",
    ):
        assert tr.get_by_name_sync(name) is not None, f"tool {name} missing after repeat"


def test_mouse_move_calls_pyautogui() -> None:
    pg = _mock_pyautogui()
    manifest = _make_manifest()
    plugin = DesktopControlPlugin(manifest)
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=pg,
    ):
        result = asyncio.run(plugin.mouse_move(10, 20))
    assert result == {"ok": True}
    pg.moveTo.assert_called_once_with(10, 20)


def test_mouse_click_defaults() -> None:
    pg = _mock_pyautogui()
    plugin = DesktopControlPlugin(_make_manifest())
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=pg,
    ):
        result = asyncio.run(plugin.mouse_click())
    assert result == {"ok": True}
    pg.click.assert_called_once_with(button="left", clicks=1)


def test_key_press_calls_press() -> None:
    pg = _mock_pyautogui()
    plugin = DesktopControlPlugin(_make_manifest())
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=pg,
    ):
        result = asyncio.run(plugin.key_press("enter"))
    assert result == {"ok": True}
    pg.press.assert_called_once_with("enter")


def test_type_text_calls_write() -> None:
    pg = _mock_pyautogui()
    plugin = DesktopControlPlugin(_make_manifest())
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=pg,
    ):
        result = asyncio.run(plugin.type_text("hello", interval=0.02))
    assert result == {"ok": True}
    pg.write.assert_called_once_with("hello", interval=0.02)


def test_screenshot_returns_base64() -> None:
    pg = _mock_pyautogui()
    plugin = DesktopControlPlugin(_make_manifest())
    pillow = MagicMock()
    from PIL import Image as _RealImage  # noqa: F401  (ensure import path exists)

    pillow.Image = MagicMock()
    with patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        return_value=pg,
    ), patch(
        "plugins.builtin.desktop_control.desktop_control._require_pillow",
        return_value=pillow,
    ):
        result = asyncio.run(plugin.screenshot())
    assert "image" in result
    assert isinstance(result["image"], str)
    # decodes back to the fake PNG bytes we wrote in _mock_pyautogui
    import base64

    assert base64.b64decode(result["image"]).startswith(b"\x89PNG")


def test_platform_guard_rejects_unknown_os() -> None:
    plugin = DesktopControlPlugin(_make_manifest())
    with patch("platform.system", return_value="FreeBSD"), pytest.raises(
        RuntimeError, match="unsupported on platform"
    ):
        plugin.load()


def test_missing_pyautogui_raises_clear_error() -> None:
    plugin = DesktopControlPlugin(_make_manifest())
    with patch("platform.system", return_value="Windows"), patch(
        "plugins.builtin.desktop_control.desktop_control._require_pyautogui",
        side_effect=RuntimeError("pyautogui is not installed"),
    ), pytest.raises(RuntimeError, match="pyautogui is not installed"):
        plugin.load()
