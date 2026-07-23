"""plugins/sdk/validator.py — static validation of a plugin on disk (CLI UX).

Checks three layers without executing the plugin:
  1. ``plugin.yaml`` is present and parses as a valid ``PluginManifest``.
  2. The declared ``.py`` entrypoint compiles (``py_compile``/``compile``).
  3. (optional, ``--strict``) declared dependencies are importable.

AXIS CONTRACT: depends on kernel.domain (PluginManifest) + plugins.loader
(_deps_resolvable) + stdlib. Pure static analysis — no plugin code runs.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kernel.domain import PluginManifest  # type: ignore[import-not-found]
from plugins.loader import _deps_resolvable

logger = logging.getLogger("hermes.plugins.validator")

_REQUIRED = ("name", "version", "entrypoint")


@dataclass
class ValidationReport:
    """Result of ``PluginValidator.validate``."""

    path: str
    ok: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
        }


class PluginValidator:
    """Static validator for a plugin folder (``plugin.yaml`` + ``.py``)."""

    def __init__(self, strict: bool = False) -> None:
        self._strict = strict

    def validate(self, path: str | Path) -> ValidationReport:
        """Validate the plugin at *path*. Returns a ``ValidationReport``."""
        root = Path(path)
        report = ValidationReport(path=str(root))

        yaml_path = root / "plugin.yaml"
        if not yaml_path.is_file():
            report.errors.append(f"missing plugin.yaml at {yaml_path}")
            report.ok = False
            return report

        try:
            raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            report.errors.append(f"plugin.yaml is not valid YAML: {exc}")
            report.ok = False
            return report

        # required manifest fields
        for field in _REQUIRED:
            if field not in raw:
                report.errors.append(f"plugin.yaml missing required field: {field}")
        if report.errors:
            report.ok = False
            return report

        try:
            manifest = PluginManifest(**raw)
        except Exception as exc:  # pydantic validation
            report.errors.append(f"plugin.yaml invalid manifest: {exc}")
            report.ok = False
            return report

        # entrypoint module compiles
        module_name = manifest.entrypoint.split(":", 1)[0]
        py_path = root / f"{module_name}.py"
        if not py_path.is_file():
            report.errors.append(f"entrypoint module not found: {py_path}")
            report.ok = False
            return report

        try:
            src = py_path.read_text(encoding="utf-8")
            compile(src, str(py_path), "exec")  # syntax only
            ast.parse(src)  # structure sanity
        except (SyntaxError, ValueError) as exc:
            report.errors.append(f"{py_path.name} does not compile: {exc}")
            report.ok = False
            return report

        # optional strict dependency check
        if self._strict and manifest.dependencies:
            if not _deps_resolvable(manifest.dependencies):
                missing = [
                    d
                    for d in manifest.dependencies
                    if __import__("importlib.util", fromlist=["find_spec"])
                    .find_spec(d)
                    is None
                ]
                report.warnings.append(f"unresolved dependencies: {missing}")

        report.ok = not report.errors
        return report
