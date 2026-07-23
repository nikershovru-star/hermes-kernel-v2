"""plugins/loader.py — discovery & instantiation of plugins from disk.

AXIS CONTRACT: depends on kernel (domain.PluginManifest, registry.PluginManifest).
Performs I/O (reads plugin.yaml, imports entrypoints). Fault-tolerant: a broken
plugin is logged and skipped; it never breaks the rest of auto_load.

plugin.yaml lives in a subdirectory; the plugin class is named by `entrypoint`
(module:attr). Dependencies are checked by importability (NOT pip install).
"""

from __future__ import annotations

import importlib
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml

from kernel.domain import PluginManifest  # type: ignore[import-not-found]
from plugins.base import BasePlugin

logger = logging.getLogger("hermes.plugins.loader")

_PLUGIN_YAML = "plugin.yaml"


def scan(directory: Path) -> list[PluginManifest]:
    """Read every plugin.yaml found directly under `directory/*/`.

    Only immediate subdirectories are scanned (one plugin per folder).
    """
    manifests: list[PluginManifest] = []
    if not directory.is_dir():
        logger.warning("scan: directory missing %s", directory)
        return manifests
    for child in sorted(directory.iterdir()):
        if not child.is_dir():
            continue
        yaml_path = child / _PLUGIN_YAML
        if not yaml_path.is_file():
            continue
        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
            manifest = PluginManifest(**raw)
            manifests.append(manifest)
        except Exception:
            logger.exception("scan: invalid plugin.yaml in %s", child)
    return manifests


def _resolve_entrypoint(entrypoint: str, plugin_dir: str | None = None) -> type[BasePlugin]:
    """Import `module:attr` and return the plugin class (must be BasePlugin).

    If ``plugin_dir`` is given, the module is loaded from that folder by file
    location (isolated import — does NOT mutate the global ``sys.path``), so a
    plugin folder can never shadow or duplicate ``kernel.*`` modules across
    test runs / reloads.
    """
    if ":" not in entrypoint:
        raise ValueError(f"entrypoint must be 'module:attr', got {entrypoint!r}")
    module_name, attr = entrypoint.split(":", 1)
    # note: we do NOT pop module_name from sys.modules here. Isolated plugin
    # imports (plugin_dir branch) register a fresh module object under the
    # same name; popping global modules here triggered a kernel.domain
    # re-import cascade that broke identity-sensitive tests elsewhere.

    if plugin_dir is not None:
        import importlib.util as _util

        # builtin plugins may declare a fully-qualified dotted entrypoint
        # (e.g. "plugins.builtin.filesystem:FileSystemPlugin") where the module
        # is a top-level file, not a folder. Prefer the isolated file import
        # only when the module file actually lives inside plugin_dir.
        file_path = os.path.join(plugin_dir, f"{module_name}.py")
        if os.path.isfile(file_path):
            # ensure canonical kernel.* modules are already in sys.modules so
            # the plugin's own `from kernel.domain import ...` reuses the same
            # module objects (prevents Event/identity collisions across tests).
            import kernel.domain  # noqa: F401
            import kernel.bus  # noqa: F401

            spec = _util.spec_from_file_location(module_name, file_path)
            if spec is None or spec.loader is None:
                raise ImportError(f"cannot build spec for {file_path}")
            module = _util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
        else:
            # fall back to normal package import (builtin plugins)
            module = importlib.import_module(module_name)
    else:
        module = importlib.import_module(module_name)

    obj: Any = getattr(module, attr, None)
    if obj is None:
        raise AttributeError(f"{entrypoint}: attribute {attr!r} not found")
    if not (isinstance(obj, type) and issubclass(obj, BasePlugin)):
        raise TypeError(f"{entrypoint} is not a BasePlugin subclass")
    return obj


def _deps_resolvable(dependencies: list[str]) -> bool:
    """Lightweight check: each dependency is importable (no pip)."""
    import importlib.util

    for dep in dependencies:
        if importlib.util.find_spec(dep) is None:
            return False
    return True


def load(manifest: PluginManifest, plugin_dir: str | None = None) -> BasePlugin:
    """Instantiate the plugin declared by `manifest`.

    ``plugin_dir`` (optional) isolates the entrypoint import by file location
    instead of mutating the global ``sys.path`` (prevents cross-test pollution
    of ``kernel.*`` modules).
    """
    if not _deps_resolvable(manifest.dependencies):
        raise RuntimeError(f"plugin {manifest.name}: unresolved dependencies {manifest.dependencies}")
    plugin_cls = _resolve_entrypoint(manifest.entrypoint, plugin_dir=plugin_dir)
    return plugin_cls(manifest)


def auto_load(paths: list[Path]) -> list[BasePlugin]:
    """Scan + load every plugin across `paths`. Broken plugins are skipped."""
    loaded: list[BasePlugin] = []
    for base in paths:
        for manifest in scan(base):
            # resolve the plugin's own folder (entrypoint module lives inside
            # it; scan only looks one level deep) — passed to load() so the
            # import is isolated and never mutates the global sys.path.
            plugin_dir = str(base / manifest.name) if base.joinpath(
                manifest.name
            ).is_dir() else str(base)
            try:
                plugin = load(manifest, plugin_dir=plugin_dir)
                loaded.append(plugin)
                logger.info("loaded plugin %s", plugin)
            except Exception:
                logger.exception("auto_load: skipped plugin %s", manifest.name)
    return loaded
