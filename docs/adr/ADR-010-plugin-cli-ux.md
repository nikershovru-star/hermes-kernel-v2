# ADR-010: Plugin CLI UX (list / validate / disable)

- **Status:** Accepted
- **Date:** 2026-07-23
- **Deciders:** Hermes Kernel v2 architecture review
- **Supersedes / Related:** ADR-002 (Plugin System), ADR-007 (Workspace Isolation)

## Context

The Plugin SDK (`plugins/sdk`) shipped `init` (scaffold) and `watch` (hot-reload)
commands in v0.8.0. Operators needed runtime introspection and safety controls:

1. **`list`** — see loaded plugins, their version, capabilities, and status.
2. **`validate <path>`** — statically check a plugin folder before load
   (manifest schema, Python compile, optional dependency resolution).
3. **`disable <name>`** — unload a plugin from the running kernel and publish a
   `plugin.disabled` event so downstream components (e.g. capability registry,
   MCP tool adapter) can drop its contributions.

The kernel already owns a `PluginRegistry` (`kernel/registry.py`) used by the
runtime and tests. The CLI must not invent a second registry — it drives the
**same** object.

## Decision

- **Single source of truth:** the CLI's `list` / `disable` commands operate on
  `kernel.registry.PluginRegistry`. No duplicate registry class in `plugins/`.
- **`PluginRegistry` extension (additive, non-breaking):**
  - `PluginInfo` frozen dataclass (`name`, `version`, `capabilities`,
    `entrypoint`, `status`).
  - `list_plugins() -> list[PluginInfo]` (sync; reads in-memory state).
  - `register_sync(manifest, instance)` — sync wrapper around the async
    `register` so the CLI (sync `argparse` context) can populate the registry
    from `auto_load`.
  - `disable(name) -> bool` (sync) — marks disabled, drops the entrypoint module
    from `sys.modules`, and publishes `plugin.disabled`.
  - `is_disabled(name)`, `enable(name)`, `load_paths(paths)`, `clear()`.
- **`PluginValidator`** (`plugins/sdk/validator.py`): three static layers,
  no plugin execution:
  1. `plugin.yaml` present + valid `PluginManifest` (required keys
     `plugin_id`/`name`, `version`, `entrypoint`, `capabilities`).
  2. entrypoint `.py` compiles (`py_compile` + `ast.parse`).
  3. entrypoint module file resolves on disk.
  - `--strict` additionally checks `dependencies` resolve via
    `importlib.util.find_spec` (warnings, not errors).
- **Sync/async boundary (ADR-007 §7):** `disable()` may run outside an event
  loop (CLI). `bus.publish` is guarded by `asyncio.get_running_loop()`; when no
  loop is active, the event is logged but not dispatched (no `RuntimeError`).
- **CLI refactor:** `main()` → `_build_parser()` + `_run_command(args) -> int`
  so commands are unit-testable without subprocess spawn.
- **Loader isolation:** `auto_load` resolves each plugin folder and loads the
  entrypoint via `importlib.util.spec_from_file_location` (isolated import),
  **never mutating the global `sys.path`**. This prevents plugin folders from
  shadowing `kernel.*` modules and avoids `Event` identity collisions across
  reloads/tests.

## Consequences

### Positive
- One registry, one event contract — no drift between runtime and CLI.
- `validate` catches the most common plugin defects before load.
- `disable` is safe: module unload + explicit event enables graceful teardown.
- Loader import isolation removes a class of cross-test / cross-plugin pollution
  bugs (verified: full suite green at 209 passed / 3 skipped).

### Negative / Trade-offs
- `register_sync` duplicates a tiny amount of `register` logic (accepted: the
  async `register` must stay for the runtime event-loop path).
- `disable` without a running loop cannot deliver `plugin.disabled` to live
  subscribers (by design — CLI is out-of-loop; the kernel's own disable path
  inside a loop does deliver it).
- Isolated `spec_from_file_location` import means a plugin cannot rely on being
  importable as a top-level package name unless it is a builtin (handled by the
  `plugin_dir` fallback to `import_module`).

### Platform limits (honest)
- `sqlite-vss` has no Windows wheels → 3 retrieval-backend tests skip on Windows
  (pass on Linux). This is unchanged by ADR-010.
- Coverage (this feature): `registry 84%`, `loader 96%`, `cli 84%`,
  `validator 87%` — all ≥ 80% gate.

## Validation
- `tests/test_plugin_registry.py` (registry + validator, 13 tests).
- `tests/test_sdk_cli.py` (`_run_command` coverage for list/validate/disable).
- `tests/test_loader_errors.py` (error paths preserved).
- Full suite: `209 passed, 3 skipped`.
