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


def _resolve_entrypoint(entrypoint: str) -> type[BasePlugin]:
    """Import `module:attr` and return the plugin class (must be BasePlugin)."""
    if ":" not in entrypoint:
        raise ValueError(f"entrypoint must be 'module:attr', got {entrypoint!r}")
    module_name, attr = entrypoint.split(":", 1)
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


def load(manifest: PluginManifest) -> BasePlugin:
    """Instantiate the plugin declared by `manifest`."""
    if not _deps_resolvable(manifest.dependencies):
        raise RuntimeError(f"plugin {manifest.name}: unresolved dependencies {manifest.dependencies}")
    plugin_cls = _resolve_entrypoint(manifest.entrypoint)
    return plugin_cls(manifest)


def auto_load(paths: list[Path]) -> list[BasePlugin]:
    """Scan + load every plugin across `paths`. Broken plugins are skipped."""
    loaded: list[BasePlugin] = []
    for base in paths:
        for manifest in scan(base):
            try:
                plugin = load(manifest)
                loaded.append(plugin)
                logger.info("loaded plugin %s", plugin)
            except Exception:
                logger.exception("auto_load: skipped plugin %s", manifest.name)
    return loaded
