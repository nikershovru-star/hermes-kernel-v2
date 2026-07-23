# ADR-016 — Agent / Plugin Unification + Unified Artifact

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review (v2.2.1)
- **Depends on:** ADR-007 (workspace isolation), ADR-011 (desktop control),
  ADR-013 (human emulation), SDK `@sdk.agent` / `@sdk.tool` (agent.py / tool.py)

## Context (the pain)

After building on the kernel, three recurring frictions surfaced:

1. **Plugin and Agent had divergent APIs.** `BasePlugin` exposes a sync
   `load/unload/get_capabilities`; agents had *no* common runtime interface —
   each builtin (DesktopControl, HumanEmulation) rolled its own `browser_start`
   / `mouse_move` style methods and registered itself ad hoc.
2. **Capabilities were called directly, no unification.** `desktop_control`
   exposes `mouse_move`/`mouse_click`; `human_emulation` exposes
   `browser_navigate`/`browser_click`/`browser_type`. No single
   "execute(capability, params)" entry point — callers must know each plugin's
   method names.
3. **Results were ad-hoc (strings / dicts), not objects.** `screenshot` returned
   `{"image": base64}`; there was no versioning, no links, no provenance. The
   question "where is the screenshot I took yesterday?" had no first-class
   answer.

## Decision

### A. Unified Agent runtime contract — `BaseAgent` (kernel/agent.py)

Introduce `BaseAgent` (ABC) mirroring `BasePlugin` but **async**, because an
agent *executes* and *returns*:

```python
class BaseAgent(ABC):
    def __init__(self, agent_entity: Agent) -> None: ...
    @abstractmethod async def start(self) -> str: ...     # returns agent_id
    @abstractmethod async def stop(self, agent_id: str) -> bool: ...
    @abstractmethod async def execute(self, agent_id: str, task: Task) -> Artifact: ...
    @abstractmethod async def status(self, agent_id: str) -> dict: ...
```

`start()` returns the `Agent` entity id, so the runtime and the declarative
registry (`AgentRegistry`) share one identity key.

**Why a NEW class and not "AgentRegistry as PluginRegistry":** `AgentRegistry`
already exists and is load-bearing — `plugins/sdk/agent.py` (`@sdk.agent`)
registers the declarative `Agent` metadata entity through it, and
`desktop_control` registers its `Agent(name="desktop_control", ...)` there.
Repurposing `AgentRegistry` to hold live instances (the PluginRegistry pattern)
would break that contract and the 246 existing tests. Instead we add
`AgentRuntime` (kernel/agent.py) as the registry of *active* `BaseAgent`
instances — the runtime counterpart to `AgentRegistry`, exactly as
`PluginRegistry` is the runtime counterpart to the `PluginManifest`.

### B. Unified result — extended `Artifact` (kernel/domain.py)

`Artifact` already existed (type/content/source). We extend it to carry the
fields the pain demanded:

```python
class Artifact(BaseEntity):
    type: str            # "screenshot" | "code" | "text" | "dataset" | ...
    content: Any         # decoded payload (was str; widened to Any)
    format: str = "text" # "png" | "py" | "md" | "json" | "base64" ...
    source: Optional[str] = None  # "agent:browser" | "plugin:desktop" | ...
    provenance: list[str] = []    # ordered chain of action/task ids
```

`content: Any` is backward-compatible (str values still validate); existing
`Artifact` rows/tests keep working. Provenance + workspace_id (inherited from
`BaseEntity`) make "the screenshot from yesterday" queryable.

### C. Unified capability dispatch — `CapabilityExecutor` (kernel/capability.py)

New class beside the existing `CapabilityRegistry` (which stays declarative).
`CapabilityExecutor.execute(capability, params, context) -> Artifact` resolves a
namespaced capability (`"browser.navigate"`, `"desktop.click"`) to an **injected
async handler** and normalizes the result into an `Artifact`.

**Why injected handlers (not direct plugin calls):** the kernel → plugins axis
forbids `kernel` importing `plugins`. So the executor receives a
`dict[capability_name, handler]` gathered by the kernel from plugin/agent
instances. The executor is pure routing + normalization — no plugin imports.

Handler return values are normalized:
- `Artifact` → returned as-is (provenance appended with `cap:<name>`).
- `dict` with `content`/`type` → mapped onto `Artifact`.
- anything else → wrapped as `Artifact(type="result", content=value)`.

### D. Reference implementation — `plugins/builtin/agents/echo_agent.py`

`EchoAgent(BaseAgent)` exercises the full lifecycle without heavy optional deps
(playwright/pyautogui live in other builtins). Real agents subclass the same
contract.

## Architecture / axis

```
kernel.agent      → [kernel.domain]          (BaseAgent, AgentRuntime)
kernel.capability → [kernel.domain, kernel.registry]  (CapabilityExecutor + Registry)
plugins.builtin.agents → [kernel, kernel.domain, plugins]  (EchoAgent reference)
```

`tach`: explicit submodule `plugins.builtin.agents` added (submodules are not
transitively inherited).

## Consequences

- Plugin and Agent now share a symmetric mental model: `PluginRegistry` (manifest
  + instance) ↔ `AgentRegistry` (metadata) + `AgentRuntime` (live instances).
- One entry point for execution: `CapabilityExecutor.execute(...)` returns a
  versioned, provenance-carrying `Artifact`.
- 3 new domain/registry/capability modules; `Artifact` extended (not replaced).
- No existing tests broken; 14 new tests (3 files).
- `CapabilityRegistry` unchanged — only additive (`CapabilityExecutor`).

## Honest notes (where it still hurts)

- **Agent loading is not yet unified with plugin loading.** Plugins load via
  `PluginRegistry.load_paths` + `loader`; agents are started explicitly via
  `AgentRuntime.start(agent)`. A future ADR should let a plugin *declare* agent
  factories so `AgentRuntime` can spin them up the way `load_paths` spins up
  plugins. We deliberately deferred this to avoid touching the loader in v2.2.1.
- **`CapabilityExecutor` needs handlers injected by the kernel.** Until the
  kernel wires plugin/agent tool methods into the executor's handler map, the
  executor is inert for unregistered capabilities (raises `KeyError`). This is
  intentional — it keeps the axis clean rather than hard-coding plugin names in
  `kernel`.
- **`Task` already existed** (name/capability/status/priority/assigned_to/
  workflow_id) and was reused as-is; no new scheduling engine was built (that is
  a future Execution Platform concern, see Hermes OS v5 vision).
