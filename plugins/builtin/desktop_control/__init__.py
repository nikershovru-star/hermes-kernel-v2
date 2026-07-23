"""plugins/builtin/desktop_control — desktop automation builtin plugin.

Re-exports the plugin class so the entrypoint
``plugins.builtin.desktop_control:DesktopControlPlugin`` resolves via a normal
package import (no file-location isolation needed for builtins).
"""

from .desktop_control import DesktopControlPlugin

__all__ = ["DesktopControlPlugin"]
