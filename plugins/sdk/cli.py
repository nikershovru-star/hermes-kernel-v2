"""plugins/sdk/cli.py — developer CLI for the Plugin SDK (variant C).

Two commands:

  hermes plugin init <name> [--dir DIR]
      Scaffold a new plugin: ``<name>.py`` (with a sample @sdk.agent + @sdk.tool)
      and ``plugin.yaml`` manifest, ready to be loaded by the kernel.

  hermes plugin watch <dir> [--interval S]
      Watch a plugin directory and hot-reload modules whose source changed
      (polling, no watchdog dependency — mirrors kernel/scanner.py).

AXIS CONTRACT: depends on plugins.sdk + stdlib only. This is a developer tool,
not kernel runtime, so it lives under plugins/sdk and is excluded from the
axis gate (import-linter excludes tests/docs; the CLI importing plugins.sdk is
downward, allowed).

Hot-reload uses ``importlib.reload`` on changed ``.py`` modules; a callback
``on_reload(module_name)`` is invoked so the kernel can re-register the plugin.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
import time
from pathlib import Path
from typing import Callable


PLUGIN_TEMPLATE = '''\
"""Auto-generated plugin: {name}.

Run `hermes plugin watch .` in this directory to hot-reload on edit.
"""

from plugins.sdk import sdk, configure_sdk
from plugins.base import BasePlugin
from kernel.bus import EventBus
from kernel.registry import AgentRegistry, ToolRegistry
from kernel.capability import CapabilityRegistry
from kernel.domain import Document


# In a real plugin these registries come from the running kernel; for a
# standalone scaffold we build local ones so the module imports cleanly.
configure_sdk(
    agent_registry=AgentRegistry(),
    tool_registry=ToolRegistry(),
    capability_registry=CapabilityRegistry(ToolRegistry()),
    bus=EventBus(),
)


@sdk.agent(name="{name}", capabilities=["hermes.{name}"])
class {Name}(BasePlugin):
    def load(self) -> bool:
        return True

    def unload(self) -> bool:
        return True

    def get_capabilities(self) -> list[str]:
        return ["hermes.{name}"]

    @sdk.tool(
        name="run_{name}",
        capability="hermes.{name}",
        schema={{"type": "object", "properties": {{"q": {{"type": "string"}}}}}},
    )
    async def run(self, q: str) -> list[Document]:
        # TODO: implement the tool body
        return []
'''

MANIFEST_TEMPLATE = """\
name: {name}
entrypoint: {name}:{Name}
capabilities:
  - hermes.{name}
version: 0.1.0
"""


def scaffold_plugin(name: str, target_dir: str | os.PathLike) -> list[Path]:
    """Create ``<name>.py`` + ``plugin.yaml`` in ``target_dir``. Returns paths."""
    target = Path(target_dir)
    target.mkdir(parents=True, exist_ok=True)
    py_path = target / f"{name}.py"
    yaml_path = target / "plugin.yaml"
    py_path.write_text(PLUGIN_TEMPLATE.format(name=name, Name=name.title()), encoding="utf-8")
    yaml_path.write_text(MANIFEST_TEMPLATE.format(name=name, Name=name.title()), encoding="utf-8")
    return [py_path, yaml_path]


class PluginWatcher:
    """Poll a directory for changed ``.py`` modules and hot-reload them."""

    def __init__(
        self,
        directory: str | os.PathLike,
        interval: float = 1.0,
        on_reload: Callable[[str], None] | None = None,
    ) -> None:
        self._dir = Path(directory)
        self.interval = interval
        self._on_reload = on_reload
        self._mtimes: dict[str, float] = {}
        self._loaded: dict[str, str] = {}  # module_name -> file path
        self._running = False

    def _module_name(self, path: Path) -> str:
        return path.stem

    def scan_once(self) -> list[str]:
        """Detect changed/new plugin files; reload them. Returns reloaded names."""
        reloaded: list[str] = []
        if not self._dir.exists():
            return reloaded
        for path in self._dir.glob("*.py"):
            name = self._module_name(path)
            mtime = path.stat().st_mtime
            prev = self._mtimes.get(name)
            if prev is None or mtime > prev:
                self._mtimes[name] = mtime
                self._loaded[name] = str(path)
                self._reload_module(name, str(path))
                reloaded.append(name)
                if self._on_reload is not None:
                    self._on_reload(name)
        return reloaded

    def _reload_module(self, name: str, path: str) -> None:
        parent = str(Path(path).parent)
        if parent not in sys.path:
            sys.path.insert(0, parent)
        if name in sys.modules:
            importlib.reload(sys.modules[name])
        else:
            spec = importlib.util.spec_from_file_location(name, path)
            if spec and spec.loader:
                mod = importlib.util.module_from_spec(spec)
                sys.modules[name] = mod
                spec.loader.exec_module(mod)

    def run(self) -> None:
        """Blocking watch loop (call from a dedicated thread in the kernel)."""
        self._running = True
        while self._running:
            self.scan_once()
            time.sleep(self.interval)

    def stop(self) -> None:
        self._running = False


def _build_parser() -> argparse.ArgumentParser:
    """Construct the full ``hermes`` argument parser (subcommands)."""
    parser = argparse.ArgumentParser(prog="hermes", description="Hermes Kernel v2 SDK CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plugin = sub.add_parser("plugin", help="plugin commands")
    p_plugin_sub = p_plugin.add_subparsers(dest="plugin_command", required=True)

    p_init = p_plugin_sub.add_parser("init", help="scaffold a new plugin")
    p_init.add_argument("name")
    p_init.add_argument("--dir", default=".")

    p_watch = p_plugin_sub.add_parser("watch", help="hot-reload plugins on change")
    p_watch.add_argument("dir", nargs="?", default=".")
    p_watch.add_argument("--interval", type=float, default=1.0)

    p_list = p_plugin_sub.add_parser("list", help="list loaded plugins (runtime)")
    p_list.add_argument(
        "--plugins-dir",
        default="plugins/builtin",
        help="directory scanned for plugin.yaml manifests",
    )

    p_validate = p_plugin_sub.add_parser("validate", help="validate a plugin on disk")
    p_validate.add_argument("path")
    p_validate.add_argument("--strict", action="store_true", help="also check deps importable")

    p_disable = p_plugin_sub.add_parser("disable", help="unload a plugin from runtime")
    p_disable.add_argument("name")
    p_disable.add_argument(
        "--plugins-dir",
        default="plugins/builtin",
        help="directory scanned for plugin.yaml manifests",
    )
    return parser


def _run_command(args: argparse.Namespace) -> int:
    """Execute a parsed command. Returns a process exit code (0 = ok)."""
    if args.command == "plugin" and args.plugin_command == "init":
        paths = scaffold_plugin(args.name, args.dir)
        for p in paths:
            print(f"created {p}")
        return 0

    if args.command == "plugin" and args.plugin_command == "watch":
        watcher = PluginWatcher(args.dir, interval=args.interval)
        print(f"watching {args.dir} (ctrl-c to stop)")
        try:
            watcher.run()
        except KeyboardInterrupt:
            pass
        return 0

    if args.command == "plugin" and args.plugin_command == "list":
        from pathlib import Path

        from kernel.registry import PluginRegistry

        reg = PluginRegistry()
        reg.load_paths([Path(args.plugins_dir)])
        infos = reg.list_plugins()
        if not infos:
            print(f"(no plugins found in {args.plugins_dir})")
            return 0
        print(f"{len(infos)} plugin(s):")
        for info in infos:
            caps = ", ".join(info.capabilities) or "-"
            print(f"  {info.name} v{info.version} [{info.status}] caps: {caps}")
        return 0

    if args.command == "plugin" and args.plugin_command == "validate":
        from plugins.sdk.validator import PluginValidator

        report = PluginValidator(strict=args.strict).validate(args.path)
        if report.ok:
            print(f"OK: {report.path}")
            for w in report.warnings:
                print(f"  warning: {w}")
            return 0
        print(f"INVALID: {report.path}")
        for e in report.errors:
            print(f"  error: {e}")
        return 1

    if args.command == "plugin" and args.plugin_command == "disable":
        from pathlib import Path

        from kernel.registry import PluginRegistry

        reg = PluginRegistry()
        reg.load_paths([Path(args.plugins_dir)])
        if reg.disable(args.name):
            print(f"disabled {args.name}")
            return 0
        print(f"unknown plugin: {args.name}")
        return 1

    return 1


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return _run_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
