"""tests/test_plugin_registry.py — kernel PluginRegistry + PluginValidator (CLI UX)."""

import sys
from pathlib import Path

import pytest

from kernel.bus import EventBus
from kernel.domain import PluginManifest
from kernel.registry import PluginInfo, PluginRegistry
from plugins.base import BasePlugin
from plugins.sdk.validator import PluginValidator, ValidationReport


# -- fixtures ----------------------------------------------------------- #
def _make_plugin(name: str, caps: list[str], entrypoint: str = "demo:Plugin") -> BasePlugin:
    manifest = PluginManifest(
        plugin_id=name,
        name=name,
        version="0.1.0",
        capabilities=caps,
        entrypoint=entrypoint,
        dependencies=[],
    )

    class _P(BasePlugin):
        def load(self) -> bool:
            return True

        def unload(self) -> bool:
            return True

        def get_capabilities(self) -> list[str]:
            return caps

    return _P(manifest)


# -- PluginInfo --------------------------------------------------------- #
def test_plugin_info_fields() -> None:
    info = PluginInfo(
        name="p1", version="1.0.0", capabilities=("a", "b"),
        entrypoint="p1:Plugin", status="loaded",
    )
    assert info.name == "p1" and info.status == "loaded"
    assert info.capabilities == ("a", "b")


# -- PluginRegistry (kernel) -------------------------------------------- #
def test_register_and_list() -> None:
    reg = PluginRegistry()
    reg.register_sync(_make_plugin("alpha", ["hermes.alpha"]).manifest,
                      _make_plugin("alpha", ["hermes.alpha"]))
    reg.register_sync(_make_plugin("beta", ["hermes.beta"]).manifest,
                      _make_plugin("beta", ["hermes.beta"]))
    infos = reg.list_plugins()
    assert len(infos) == 2
    assert {i.name for i in infos} == {"alpha", "beta"}
    assert all(i.status == "loaded" for i in infos)


def test_get_sync_returns_instance() -> None:
    reg = PluginRegistry()
    p = _make_plugin("alpha", ["hermes.alpha"])
    reg.register_sync(p.manifest, p)
    assert reg.get_sync("alpha") is p
    assert reg.get_sync("missing") is None


def test_disable_marks_and_unloads_module() -> None:
    bus = EventBus()
    reg = PluginRegistry(bus=bus)
    reg.register_sync(_make_plugin("alpha", ["hermes.alpha"], entrypoint="demomod:Plugin").manifest,
                      _make_plugin("alpha", ["hermes.alpha"], entrypoint="demomod:Plugin"))
    sys.modules["demomod"] = type(sys)("demomod")  # sentinel
    assert "demomod" in sys.modules
    assert reg.disable("alpha") is True
    assert reg.is_disabled("alpha")
    assert "demomod" not in sys.modules
    assert reg.get_sync("alpha") is None


def test_disable_unknown_returns_false() -> None:
    reg = PluginRegistry()
    assert reg.disable("nope") is False


def test_enable_clears_disabled() -> None:
    reg = PluginRegistry()
    p = _make_plugin("alpha", ["hermes.alpha"])
    reg.register_sync(p.manifest, p)
    reg.disable("alpha")
    assert reg.is_disabled("alpha")
    assert reg.enable("alpha") is True
    assert not reg.is_disabled("alpha")


def test_load_paths_via_loader(tmp_path: Path) -> None:
    from plugins.sdk.cli import scaffold_plugin

    scaffold_plugin("demo", tmp_path / "demo")
    reg = PluginRegistry()
    loaded = reg.load_paths([tmp_path])
    assert len(loaded) == 1
    infos = reg.list_plugins()
    assert infos[0].name == "demo"
    assert infos[0].status == "loaded"


# -- PluginValidator ----------------------------------------------------- #
def _write_plugin(root: Path, missing_py: bool = False) -> Path:
    (root / "plugin.yaml").write_text(
        "plugin_id: demo\nname: demo\nversion: 0.1.0\n"
        "entrypoint: demo:Plugin\ncapabilities:\n  - hermes.demo\n",
        encoding="utf-8",
    )
    if not missing_py:
        (root / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    return root


def test_validate_ok(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    report = PluginValidator().validate(tmp_path)
    assert isinstance(report, ValidationReport)
    assert report.ok is True
    assert report.errors == []


def test_validate_missing_yaml(tmp_path: Path) -> None:
    report = PluginValidator().validate(tmp_path)
    assert report.ok is False
    assert any("plugin.yaml" in e for e in report.errors)


def test_validate_missing_required_field(tmp_path: Path) -> None:
    (tmp_path / "plugin.yaml").write_text("name: demo\n", encoding="utf-8")
    (tmp_path / "demo.py").write_text("VALUE = 1\n", encoding="utf-8")
    report = PluginValidator().validate(tmp_path)
    assert report.ok is False
    assert any("plugin_id" in e or "entrypoint" in e for e in report.errors)


def test_validate_bad_python(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    (tmp_path / "demo.py").write_text("def (:\n", encoding="utf-8")
    report = PluginValidator().validate(tmp_path)
    assert report.ok is False
    assert any("compile" in e for e in report.errors)


def test_validate_missing_py_module(tmp_path: Path) -> None:
    _write_plugin(tmp_path, missing_py=True)
    (tmp_path / "plugin.yaml").write_text(
        "plugin_id: demo\nname: demo\nversion: 0.1.0\n"
        "entrypoint: nope:Plugin\ncapabilities:\n  - hermes.demo\n",
        encoding="utf-8",
    )
    report = PluginValidator().validate(tmp_path)
    assert report.ok is False
    assert any("entrypoint module" in e for e in report.errors)


def test_validate_strict_unresolved_dep(tmp_path: Path) -> None:
    _write_plugin(tmp_path)
    (tmp_path / "plugin.yaml").write_text(
        "plugin_id: demo\nname: demo\nversion: 0.1.0\n"
        "entrypoint: demo:Plugin\ncapabilities:\n  - hermes.demo\n"
        "dependencies:\n  - this_module_does_not_exist_xyz\n",
        encoding="utf-8",
    )
    report = PluginValidator(strict=True).validate(tmp_path)
    assert report.ok is True  # deps only warn under strict
    assert any("unresolved" in w for w in report.warnings)
