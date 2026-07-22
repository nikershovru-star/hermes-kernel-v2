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
from kernel.bus import EventBus
from kernel.registry import (AgentRegistry, ToolRegistry, CapabilityRegistry)
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
class {Name}:
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
plugin_id: {name}
name: {name}
entrypoint: {name}.py
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
    yaml_path.write_text(MANIFEST_TEMPLATE.format(name=name), encoding="utf-8")
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hermes", description="Hermes Kernel v2 SDK CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("plugin", help="plugin commands")
    p_plugin_sub = p_init.add_subparsers(dest="plugin_command", required=True)
    p_init_cmd = p_plugin_sub.add_parser("init", help="scaffold a new plugin")
    p_init_cmd.add_argument("name")
    p_init_cmd.add_argument("--dir", default=".")

    p_watch = p_plugin_sub.add_parser("watch", help="hot-reload plugins on change")
    p_watch.add_argument("dir", nargs="?", default=".")
    p_watch.add_argument("--interval", type=float, default=1.0)

    args = parser.parse_args(argv)

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
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
