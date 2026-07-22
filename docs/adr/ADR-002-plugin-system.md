# ADR-002 — Plugin System

- **Status:** accepted
- **Date:** 2026-07-22
- **Deciders:** Nikita (architect)
- **Depends on:** [ADR-001](ADR-001-kernel-architecture.md)

## Context

The kernel must be extensible by third parties without modifying core code.
Two audiences exist: (1) **operators** who drop packaged plugins into a
directory, and (2) **authors** who write agents/tools declaratively. We need a
loading mechanism that is fault-tolerant (one broken plugin must not crash the
kernel) and an authoring API that is terse and idempotent.

## Decision

### 1. Plugin contract (`plugins/base.py`)

`BasePlugin` (ABC) — every packaged plugin subclasses it:

```python
class BasePlugin(ABC):
    def __init__(self, manifest: PluginManifest): ...
    async def load(self) -> bool: ...
    async def unload(self) -> bool: ...
    def get_capabilities(self) -> list[str]: ...
    # properties: name, manifest
```

### 2. Manifest (`plugin.yaml`)

Declarative metadata parsed into `kernel.domain.PluginManifest` (the manifest is
a **domain entity**, not a loader concern — hence it lives in `domain.py`):

```yaml
name: filesystem
version: "0.1.0"
capabilities: ["hermes.fs"]
entrypoint: "plugins.builtin.filesystem:FilesystemPlugin"
dependencies: []
```

### 3. Loader (`plugins/loader.py`)

- `scan(dir)` — enumerate immediate sub-directories containing `plugin.yaml`.
- `_resolve_entrypoint("pkg.mod:Class")` — `importlib` resolve; validates the
  target is a `BasePlugin` subclass (else `TypeError`).
- `_deps_resolvable(manifest)` — `importlib.util.find_spec` (no `pip` calls).
- `load` / `auto_load` — **fault-tolerant**: a broken plugin is logged and
  skipped, never propagated.

### 4. Plugin SDK (`plugins/sdk/`) — declarative authoring

Four decorators, all **idempotent** (re-construction never duplicates entries):

| Decorator | Registers into | Notes |
|-----------|----------------|-------|
| `@agent(name, capabilities)` | `AgentRegistry` | class decorator; harvests the members below on `__init__` and injects `__bus__`, `__tool_registry__`, `__capability_registry__` |
| `@tool(name, capability, schema)` | `ToolRegistry` | method becomes the tool handler; `schema` = JSON Schema (`{}` default) |
| `@on_event(type)` | `EventBus` | method auto-subscribed at agent construction |
| `@capability(name, tools)` | `CapabilityRegistry` | `tools` = list of tool **names** (lazy resolution, not objects) |

**Injection is synchronous.** Agent constructors run on the main thread and
cannot `await`, so the SDK uses the registries' `*_sync` fast-path (see ADR-001).
`configure_sdk(agent_registry=…, tool_registry=…, capability_registry=…, bus=…)`
must be called once before any `@agent` class is instantiated.

#### Example

```python
from plugins.sdk import sdk, configure_sdk

@sdk.agent(name="researcher", capabilities=["hermes.search"])
class Researcher:
    @sdk.tool(name="web_search", capability="hermes.search",
              schema={"type": "object", "properties": {"q": {"type": "string"}}})
    async def search(self, q: str) -> list: ...

    @sdk.on_event("document.parsed")
    async def handle_doc(self, event): ...

    @sdk.capability(name="hermes.custom", tools=["web_search"])
    def declare_custom(self): pass  # decorator registers the capability
```

## Consequences

**Positive**

- Authors write zero registry boilerplate; the decorator harvest wires
  everything at construction.
- One broken plugin cannot take down the kernel (loader fault-tolerance:
  `plugins/loader.py` 97% covered, error-paths in `test_loader_errors.py`).
- SDK is 95% covered (`test_sdk.py`, 5 tests).

**Negative / trade-offs**

- `@tool` must remember the underlying method name separately from the public
  tool name (fixed footgun: harvesting stored `method=func.__name__`).
- SDK requires a global `configure_sdk` call — an implicit ordering dependency,
  mitigated by a clear `RuntimeError` if used unconfigured.

## Related

- [ADR-001 — Kernel Architecture](ADR-001-kernel-architecture.md)
- [ADR-003 — MCP Integration](ADR-003-mcp-integration.md)
