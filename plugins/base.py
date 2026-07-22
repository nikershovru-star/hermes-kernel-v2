"""plugins/base.py — Plugin contract for Hermes Kernel v2.

AXIS CONTRACT: depends on kernel (domain.PluginManifest). No I/O here — loading
from disk is the loader's job. A plugin instance carries its manifest; the
registry stores (manifest, instance) pairs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from kernel.domain import PluginManifest  # type: ignore[import-not-found]


class BasePlugin(ABC):
    """Abstract plugin. Subclass and implement load/unload/get_capabilities."""

    def __init__(self, manifest: PluginManifest) -> None:
        self._manifest = manifest

    # -- contract --------------------------------------------------------- #
    @abstractmethod
    def load(self) -> bool:
        """Initialize plugin resources. Return True on success."""

    @abstractmethod
    def unload(self) -> bool:
        """Release resources. Return True on success."""

    @abstractmethod
    def get_capabilities(self) -> list[str]:
        """Declare the capability namespaces this plugin provides."""

    # -- introspection ---------------------------------------------------- #
    @property
    def manifest(self) -> PluginManifest:
        return self._manifest

    @property
    def name(self) -> str:
        return self._manifest.name

    @property
    def capabilities(self) -> list[str]:
        # Default: manifest-declared; subclasses may override get_capabilities().
        return list(self._manifest.capabilities)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.name} v{self._manifest.version}>"


__all__ = ["BasePlugin", "PluginManifest"]
