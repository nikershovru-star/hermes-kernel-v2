"""tests/test_loader_errors.py — error-path coverage for loader internals."""

from pathlib import Path

import pytest

from kernel import domain, registry
from plugins import loader


# --- scan() error paths ---------------------------------------------------- #
def test_scan_missing_directory_logs_and_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    assert loader.scan(missing) == []


def test_scan_skips_non_dir_entries(tmp_path: Path) -> None:
    # a plain file (not a dir) inside the scan root must be skipped
    (tmp_path / "not_a_plugin.txt").write_text("x", encoding="utf-8")
    assert loader.scan(tmp_path) == []


def test_scan_skips_dir_without_plugin_yaml(tmp_path: Path) -> None:
    (tmp_path / "empty_plugin").mkdir()
    assert loader.scan(tmp_path) == []


def test_scan_invalid_yaml_is_skipped(tmp_path: Path) -> None:
    bad = tmp_path / "broken"
    bad.mkdir()
    (bad / "plugin.yaml").write_text(
        "name: broken\ncapabilities: [x]\nentrypoint: a:b\n",  # missing version
        encoding="utf-8",
    )
    assert loader.scan(tmp_path) == []


# --- _resolve_entrypoint() error paths ------------------------------------- #
def test_resolve_entrypoint_missing_colon() -> None:
    with pytest.raises(ValueError):
        loader._resolve_entrypoint("no_colon_here")


def test_resolve_entrypoint_unknown_module() -> None:
    with pytest.raises(ModuleNotFoundError):
        loader._resolve_entrypoint("no_such_module_xyz:Plugin")


def test_resolve_entrypoint_missing_attr() -> None:
    with pytest.raises(AttributeError):
        loader._resolve_entrypoint("plugins.builtin.filesystem:DoesNotExist")


def test_resolve_entrypoint_not_a_baseplugin() -> None:
    # Tool is a pydantic model, not a BasePlugin subclass -> TypeError
    with pytest.raises(TypeError):
        loader._resolve_entrypoint("kernel.domain:Tool")


# --- _deps_resolvable() error path ----------------------------------------- #
def test_deps_resolvable_false_for_missing_module() -> None:
    assert loader._deps_resolvable(["this_module_does_not_exist_123"]) is False


def test_deps_resolvable_true_for_stdlib() -> None:
    assert loader._deps_resolvable(["os", "sys"]) is True


# --- load() error path (unresolved deps) ----------------------------------- #
def test_load_raises_on_unresolved_dependency() -> None:
    m = registry.PluginManifest(
        name="needsdep", version="1", capabilities=[],
        entrypoint="plugins.builtin.filesystem:FilesystemPlugin",
        dependencies=["module_that_does_not_exist_999"],
    )
    with pytest.raises(RuntimeError):
        loader.load(m)


# --- auto_load() error path (broken entrypoint skipped) -------------------- #
def test_auto_load_skips_bad_entrypoint(tmp_path: Path) -> None:
    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "plugin.yaml").write_text(
        "name: broken\nversion: 0\ncapabilities: []\nentrypoint: no_mod:Plugin\ndependencies: []\n",
        encoding="utf-8",
    )
    loaded = loader.auto_load([tmp_path])
    assert loaded == []  # broken plugin skipped, no crash
