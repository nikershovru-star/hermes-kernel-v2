"""Example builtin plugin: filesystem read/write capability."""

from __future__ import annotations

from pathlib import Path

from kernel.domain import PluginManifest  # type: ignore[import-not-found]
from plugins.base import BasePlugin


class FilesystemPlugin(BasePlugin):
    """Provides hermes.fs.read / hermes.fs.write over the local tree."""

    def load(self) -> bool:
        # No external resources needed for this demo.
        return True

    def unload(self) -> bool:
        return True

    def get_capabilities(self) -> list[str]:
        return ["hermes.fs.read", "hermes.fs.write"]

    # -- example tool surface (registered later via ToolRegistry) --------- #
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def write(self, path: str, content: str) -> None:
        Path(path).write_text(content, encoding="utf-8")
