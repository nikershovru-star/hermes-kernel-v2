"""plugins/sdk/tool.py — @tool decorator (marks a method as a Tool handler)."""

from __future__ import annotations

from typing import Any, Callable

# marker attribute set on wrapped methods
_TOOL_MARKER = "__sdk_tool__"


def tool(name: str, capability: str, schema: dict[str, Any] | None = None) -> Callable:
    """Mark a class method as a kernel Tool.

    The method becomes the tool's handler; metadata (name/capability/schema)
    is attached and later harvested by @agent into the ToolRegistry.
    """

    def decorate(func: Callable) -> Callable:
        setattr(
            func,
            _TOOL_MARKER,
            {
                "name": name,
                "capability": capability,
                "schema": schema or {},
                "method": func.__name__,
            },
        )
        return func

    return decorate


def get_tools(cls) -> list[dict[str, Any]]:
    """Harvest @tool metadata from a class (used by @agent)."""
    found: list[dict[str, Any]] = []
    for attr in vars(cls).values():
        meta = getattr(attr, _TOOL_MARKER, None)
        if meta is not None:
            found.append(meta)
    return found
