# ADR-018 — Capability Handler Auto-Discovery

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review (v2.4.0)
- **Depends on:** ADR-017 (Event Platform + DesktopAgent + manual `CapabilityExecutor`
  wiring), ADR-016 (`BaseAgent`, `CapabilityExecutor`), ADR-007 (axis)

## Context (the deferred pain from ADR-017)

ADR-017 shipped `CapabilityExecutor` with **manual** handler wiring
(`register_agent` / `register_handler`) and explicitly deferred auto-discovery
to this ADR. Without it, every new plugin/agent required a hand-written bootstrap
line to make its capabilities callable via `CapabilityExecutor.execute()` — the
exact "register Agent separate from Plugin" pain we set out to kill.

## Decision

Add a **post-load reflection** step (`kernel/discovery.py`) that the kernel runs
once after loading all plugin/agent instances:

```python
def discover_handlers(instances, executor) -> int:
    for inst in instances:
        if isinstance(inst, BaseAgent):
            executor.register_agent(inst)          # agent -> Task-routing handler
        else:
            for meta in get_tools(type(inst)):     # plugin -> @sdk.tool methods
                executor.register_handler(meta["capability"],
                                          _make_plugin_handler(inst, meta["method"]))
    return wired_count
```

`CapabilityExecutor.autodiscover(instances)` is a thin convenience wrapper.

### Why reflection on instances (not module scanning)

The kernel→plugins axis is absolute. We therefore do **not** scan plugin
modules at import time (that would force `kernel` to import `plugins`). Instead
the kernel passes the **already-loaded instances** it holds after
`PluginRegistry.load_paths`. `discovery.py` only imports `kernel.agent` (to
type-check `BaseAgent`) and `plugins.sdk.tool.get_tools` — the latter is imported
**lazily inside the function** to avoid an import cycle
(`plugins.sdk` → `kernel.capability` → `kernel.discovery` → `plugins.sdk`).

### Handler adaptation

* **Agent capabilities** reuse the v2.3.0 `register_agent` path: capability →
  `Task` → `agent.execute(agent_id, task)` → `Artifact`.
* **Plugin `@sdk.tool` methods** become `async handler(params, context)` adapters
  that call `method(**params)` (reserved keys like `context` are only forwarded
  if the method accepts them, via `inspect.signature`). The return value is
  normalized by `CapabilityExecutor._normalize` into a unified `Artifact`.

## Architecture / axis

`kernel.discovery` → `[kernel.domain, kernel.agent]`   (reads `@sdk.tool` marker
by string key — NO `plugins` import)
kernel.capability  → [kernel.domain, kernel.events, kernel.agent, kernel.discovery]
plugins.sdk.tool   → []                                (marker reader, no kernel import)
```

`tach` requires `kernel.discovery` to depend on `kernel.domain` + `kernel.agent`
only. It reads the `@sdk.tool` marker attribute (`"__sdk_tool__"`) **directly by
string key** — never importing `plugins.sdk.tool` — so the kernel→plugins axis is
fully preserved (no tach exception, no runtime import cycle).

## Consequences

- +6 tests (`test_capability_discovery.py`): plugin `@sdk.tool` wiring, param
  forwarding, BaseAgent wiring, mixed instances, idempotency.
- Manual `register_agent` / `register_handler` still work (explicit override).
- A single `executor.autodiscover(instances)` replaces N bootstrap lines.

## Honest notes

- **No plugin import in `kernel`.** `discover_handlers` reads the `@sdk.tool`
  marker attribute (`"__sdk_tool__"`) by string key directly from class methods
  instead of importing `plugins.sdk.tool.get_tools`. This keeps the absolute
  kernel→plugins axis intact (tach stays green, no import cycle). The marker key
  is duplicated intentionally as a string literal with a comment pointing at the
  SDK source of truth.
- **No module crawling.** Discovery is explicitly instance-based; a plugin must
  be loaded (its instance created) before its capabilities appear. This is
  intentional — it keeps the kernel free of plugin imports and matches the
  existing `PluginRegistry.load_paths(paths, loader)` design (loader injected).
- **`get_tools` harvests class-level markers only** (`vars(cls)`). Instance-
  assigned handlers are not discovered — consistent with how `@sdk.tool` is used.
- **Manual `register_agent` / `register_handler` remain** for explicit overrides;
  auto-discovery is idempotent (re-running overwrites by capability name).
