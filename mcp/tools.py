"""mcp/tools.py — adapt kernel.domain.Tool to MCP Tool schema + arg validation.

AXIS CONTRACT: depends on kernel.domain only. Pure adapter, no I/O, no bus.
"""

from __future__ import annotations

from typing import Any

from kernel.domain import Tool


class MCPToolAdapter:
    """Convert domain Tool <-> MCP Tool JSON and validate call arguments."""

    @staticmethod
    def to_mcp_schema(tool: Tool) -> dict:
        """Tool -> MCP `tools/list` entry."""
        return {
            "name": tool.name,
            "description": (tool.metadata.get("description") or tool.capability),
            "inputSchema": tool.input_schema or {"type": "object", "properties": {}},
        }

    @staticmethod
    def from_mcp_call(name: str, arguments: dict) -> tuple[Tool, dict]:
        """Resolve a tool by name + raw args.

        NOTE: this adapter validates against a schema, but the authoritative
        lookup lives in ToolRegistry. We return (placeholder_tool, args) here
        for callers that don't hold the registry; the server uses
        ToolRegistry.get_by_name instead. Kept for symmetry / standalone use.
        """
        # Minimal Tool view for validation-only contexts
        tool = Tool(name=name, capability="", input_schema=arguments.get("__schema__", {}))
        return tool, arguments

    @staticmethod
    def validate_arguments(tool: Tool, args: dict) -> bool:
        """Validate `args` against tool.input_schema (JSON Schema, partial support).

        Supports the common subset: required props, and type checks for the
        declared properties. Returns True if args satisfy the schema.
        """
        schema = tool.input_schema or {}
        if not isinstance(args, dict):
            return False
        props: dict[str, Any] = schema.get("properties", {})
        required: list[str] = schema.get("required", [])
        # required presence
        for r in required:
            if r not in args:
                return False
        # type checks for provided props
        type_map = {
            "string": str, "integer": int, "number": (int, float),
            "boolean": bool, "object": dict, "array": list,
        }
        for key, val in args.items():
            if key not in props:
                # unknown keys allowed unless schema is strict (additionalProperties=false)
                if schema.get("additionalProperties") is False:
                    return False
                continue
            expected = props[key].get("type")
            if expected and expected in type_map:
                if not isinstance(val, type_map[expected]):
                    return False
        return True
