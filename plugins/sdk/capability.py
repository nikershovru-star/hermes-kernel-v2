"""plugins/sdk/capability.py — @capability decorator (marks a method as a Capability decl)."""

from __future__ import annotations

from typing import Any, Callable

_CAP_MARKER = "__sdk_capability__"


def capability(name: str, tools: list[str] | None = None) -> Callable:
    """Mark a class method as a Capability declaration.

    `tools` is a list of Tool *names* (strings) — resolved lazily, not objects.
    The decorator registers the Capability at agent-construction time.
    """

    def decorate(func: Callable) -> Callable:
        setattr(func, _CAP_MARKER, {"name": name, "tools": tools or []})
        return func

    return decorate


def get_capabilities(cls) -> list[dict[str, Any]]:
    """Harvest @capability metadata from a class (used by @agent)."""
    found: list[dict[str, Any]] = []
    for attr in vars(cls).values():
        meta = getattr(attr, _CAP_MARKER, None)
        if meta is not None:
            found.append(meta)
    return found
