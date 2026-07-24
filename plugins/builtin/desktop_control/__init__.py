"""plugins.builtin.desktop_control — desktop automation builtin package.

Re-exports both the legacy ``DesktopControlPlugin`` (BasePlugin, MCP tools) and
the new event-driven ``DesktopAgent`` (BaseAgent, ADR-017) so entrypoints resolve
via normal package imports.

  plugins.builtin.desktop_control:DesktopControlPlugin
  plugins.builtin.desktop_control:DesktopAgent

``DesktopVision`` lives in the ``vision`` submodule (its own tach module) and is
imported directly from there to keep the package root from depending on its own
submodule.
"""

from .behavior import BehaviorEngine
from .desktop_agent import DesktopAgent
from .desktop_control import DesktopControlPlugin
from .human_profile import HumanProfileStore

__all__ = ["DesktopControlPlugin", "DesktopAgent", "BehaviorEngine", "HumanProfileStore"]
