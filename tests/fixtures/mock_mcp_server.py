import sys
import json

"""Minimal mock MCP server (JSON-RPC 2.0 over stdio) for Hermes Kernel tests.

Speaks just enough of the MCP protocol to exercise MCPClient:
  - initialize        -> serverInfo + capabilities
  - notifications/init -> ignored (no response)
  - tools/list        -> two tools: echo, add
  - tools/call        -> echo/add results, or error for unknown tool
Line-delimited JSON on stdin/stdout.
"""

def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main() -> None:
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if method == "initialize":
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {
                    "serverInfo": {"name": "mock-mcp", "version": "0.1.0"},
                    "capabilities": {"tools": {}},
                },
            })
        elif method == "tools/list":
            _send({
                "jsonrpc": "2.0", "id": mid,
                "result": {"tools": [
                    {
                        "name": "echo",
                        "description": "echo text back",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    },
                    {
                        "name": "add",
                        "description": "add two numbers",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                "a": {"type": "number"},
                                "b": {"type": "number"},
                            },
                        },
                    },
                ]},
            })
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "echo":
                _send({
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"content": [
                        {"type": "text", "text": args.get("text", "")}
                    ]},
                })
            elif name == "add":
                _send({
                    "jsonrpc": "2.0", "id": mid,
                    "result": {"content": [
                        {"type": "text",
                         "text": str(args.get("a", 0) + args.get("b", 0))}
                    ]},
                })
            else:
                _send({
                    "jsonrpc": "2.0", "id": mid,
                    "error": {"code": -32000, "message": f"unknown tool {name}"},
                })
        # notifications/* have no id -> no response


if __name__ == "__main__":
    main()
