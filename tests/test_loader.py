"""tests/test_loader.py — plugin discovery, load, auto_load, fault tolerance."""

from pathlib import Path

import pytest

from kernel import registry
from plugins import base, loader

BUILTIN = Path(__file__).resolve().parent.parent / "plugins" / "builtin"


def test_scan_finds_filesystem() -> None:
    mans = loader.scan(BUILTIN)
    names = {m.name for m in mans}
    assert "filesystem" in names


def test_load_filesystem_plugin() -> None:
    mans = loader.scan(BUILTIN)
    fs = next(m for m in mans if m.name == "filesystem")
    plugin = loader.load(fs)
    assert isinstance(plugin, base.BasePlugin)
    assert plugin.name == "filesystem"
    assert plugin.get_capabilities() == ["hermes.fs.read", "hermes.fs.write"]
    assert plugin.load() is True
    assert plugin.unload() is True


def test_auto_load_returns_instances() -> None:
    plugins = loader.auto_load([BUILTIN])
    assert any(p.name == "filesystem" for p in plugins)


def test_load_missing_module_skips_gracefully(caplog) -> None:
    # a manifest whose entrypoint cannot be imported must be skipped, not crash
    bad = registry.PluginManifest(
        name="broken", version="0", capabilities=[], entrypoint="no_such_module:Plugin"
    )
    with pytest.raises(Exception):
        loader.load(bad)  # load() raises; auto_load swallows it


def test_auto_load_skips_broken(tmp_path: Path) -> None:
    # create a broken plugin dir; auto_load must skip it and still load valid ones
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.yaml").write_text(
        "name: broken\nversion: 0\ncapabilities: []\nentrypoint: no_such_module:Plugin\ndependencies: []\n",
        encoding="utf-8",
    )
    # valid one copied from builtin
    import shutil

    shutil.copytree(BUILTIN / "filesystem", tmp_path / "filesystem")
    loaded = loader.auto_load([tmp_path])
    assert any(p.name == "filesystem" for p in loaded)
    assert all(p.name != "broken" for p in loaded)
