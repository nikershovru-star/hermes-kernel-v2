"""tests/test_sdk_cli.py — Plugin SDK CLI: scaffold + hot-reload watch (variant C)."""

import importlib
import os
import sys
import time

import pytest

from plugins.sdk.cli import PluginWatcher, scaffold_plugin


def test_scaffold_creates_files(tmp_path) -> None:
    paths = scaffold_plugin("demo", tmp_path / "demo")
    names = {p.name for p in paths}
    assert names == {"demo.py", "plugin.yaml"}
    for p in paths:
        assert p.exists()
        assert p.read_text(encoding="utf-8").strip()


def test_scaffold_valid_python(tmp_path) -> None:
    scaffold_plugin("demo", tmp_path / "demo")
    py = tmp_path / "demo" / "demo.py"
    src = py.read_text(encoding="utf-8")
    # the scaffold must be syntactically valid (compile, not execute kernel)
    compile(src, str(py), "exec")
    assert "@sdk.agent" in src
    assert "configure_sdk" in src


def test_watcher_detects_change_and_reloads(tmp_path) -> None:
    # drop an initial valid plugin module
    mod_file = tmp_path / "plg.py"
    mod_file.write_text(
        "VALUE = 1\n", encoding="utf-8"
    )
    reloaded: list[str] = []
    watcher = PluginWatcher(tmp_path, interval=0.05, on_reload=reloaded.append)
    # first scan loads the module
    watcher.scan_once()
    assert "plg" in sys.modules
    assert sys.modules["plg"].VALUE == 1

    # mutate the source on disk, bump mtime so the watcher detects it
    time.sleep(0.05)
    mod_file.write_text("VALUE = 2\n", encoding="utf-8")
    new_time = time.time() + 1.0  # guarantee mtime advances past the snapshot
    os.utime(mod_file, (new_time, new_time))
    watcher.scan_once()
    assert sys.modules["plg"].VALUE == 2
    assert "plg" in reloaded

    # cleanup sys.modules so other tests are unaffected
    sys.modules.pop("plg", None)


# -- CLI _run_command coverage (list / validate / disable) ------------ #
def test_cli_list_empty(tmp_path, capsys) -> None:
    from kernel.registry import PluginRegistry
    from plugins.sdk.cli import _build_parser, _run_command

    parser = _build_parser()
    args = parser.parse_args(["plugin", "list", "--plugins-dir", str(tmp_path)])
    rc = _run_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "no plugins found" in out


def test_cli_list_after_scaffold(tmp_path, capsys) -> None:
    from kernel.registry import PluginRegistry
    from plugins.sdk.cli import _build_parser, _run_command, scaffold_plugin

    scaffold_plugin("demo", tmp_path / "demo")
    parser = _build_parser()
    args = parser.parse_args(["plugin", "list", "--plugins-dir", str(tmp_path)])
    rc = _run_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out and "[loaded]" in out


def test_cli_validate_ok(tmp_path, capsys) -> None:
    from plugins.sdk.cli import _build_parser, _run_command, scaffold_plugin

    scaffold_plugin("demo", tmp_path / "demo")
    parser = _build_parser()
    args = parser.parse_args(["plugin", "validate", str(tmp_path / "demo")])
    rc = _run_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "OK" in out


def test_cli_validate_missing_yaml(tmp_path, capsys) -> None:
    from plugins.sdk.cli import _build_parser, _run_command

    parser = _build_parser()
    args = parser.parse_args(["plugin", "validate", str(tmp_path)])
    rc = _run_command(args)
    assert rc == 1
    out = capsys.readouterr().out
    assert "INVALID" in out


def test_cli_validate_strict(tmp_path, capsys) -> None:
    from plugins.sdk.cli import _build_parser, _run_command, scaffold_plugin

    scaffold_plugin("demo", tmp_path / "demo")
    # inject an unresolvable dep
    yaml_path = tmp_path / "demo" / "plugin.yaml"
    yaml_path.write_text(
        "plugin_id: demo\nname: demo\nentrypoint: demo:Plugin\n"
        "capabilities:\n  - hermes.demo\nversion: 0.1.0\n"
        "dependencies:\n  - no_such_mod_xyz\n",
        encoding="utf-8",
    )
    parser = _build_parser()
    args = parser.parse_args(["plugin", "validate", str(tmp_path / "demo"), "--strict"])
    rc = _run_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "warning" in out


def test_cli_disable_unknown(tmp_path, capsys) -> None:
    from plugins.sdk.cli import _build_parser, _run_command

    parser = _build_parser()
    args = parser.parse_args(
        ["plugin", "disable", "nope", "--plugins-dir", str(tmp_path)]
    )
    rc = _run_command(args)
    assert rc == 1
    out = capsys.readouterr().out
    assert "unknown plugin" in out


def test_cli_disable_and_list(tmp_path, capsys) -> None:
    from kernel.registry import PluginRegistry
    from plugins.sdk.cli import _build_parser, _run_command, scaffold_plugin

    scaffold_plugin("demo", tmp_path / "demo")
    parser = _build_parser()
    args = parser.parse_args(
        ["plugin", "disable", "demo", "--plugins-dir", str(tmp_path)]
    )
    rc = _run_command(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "disabled demo" in out
