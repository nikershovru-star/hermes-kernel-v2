"""plugins.builtin.human_emulation — Human Emulation builtin plugin.

Re-exports the plugin class so the entrypoint
``plugins.builtin.human_emulation:HumanEmulationPlugin`` resolves via a normal
package import.
"""

from .human_emulation import HumanEmulationPlugin

__all__ = ["HumanEmulationPlugin"]
