# ADR-026 — Plugin Marketplace & Multi-node

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.12.0)
- **Depends on:** ADR-017 (Event Platform), ADR-023 (Swarm / Teams), ADR-025 (Knowledge Graph)

---

## Context

To grow beyond built-in plugins, the kernel needs (1) a **plugin marketplace**
to discover/install/validate third-party packages and (2) optional **multi-node**
coordination so several kernel instances can form a logical cluster. ADR-007's
`PluginRegistry` already tracks *loaded* plugin instances in-proc; it is not a
discovery/install surface. ADR-023's `kernel.domain.NodeInfo` models a logical
node but has no membership/broadcast API.

## Decision

- **`kernel/marketplace_domain.py`** *(new, isolated module)* — domain models:
  `PluginSource`, `PluginStatus` enums; `PluginPackage`, `CatalogEntry`,
  `NodeInfo`, `ClusterTopology`. **Note:** `kernel/domain.py` already defines an
  ADR-023 `NodeInfo` (fields `load_score` / `last_seen`) and `kernel/registry.py`
  a `PluginRegistry`. To avoid clobbering ADR-023/ADR-007 and regressing the
  551-test baseline, the ADR-026 models live in their own axis-clean module
  (imported as `from kernel.marketplace_domain import ...`).
- **`kernel/events.py`** — 5 new events (DomainEvent convention): `PluginDiscovered`,
  `PluginInstalled`, `PluginInstallFailed`, `NodeJoined`, `NodeLeft`.
- **`kernel/marketplace.py`** — `PluginMarketplace` (async): `discover` (fetches
  a remote JSON catalog via injected `http_client`), `install`/`uninstall`
  (status transitions + events), `validate_package` (SHA-256 checksum + dependency
  check), `list_installed`/`list_available`, `register_local`. All `event_bus`/
  `event_store`/`registry`/`clock`/`rng`/`sleep`/`http_client` injectable.
  Axis: `kernel.marketplace_domain` + `kernel.events` only.
- **`kernel/cluster.py`** — `ClusterManager` (full, not a stub): `join_cluster`,
  `leave_cluster`, `get_topology`, `elect_leader` (oldest node by
  `last_heartbeat`), `heartbeat`, `prune_timed_out`, `broadcast` (via injected
  `transport`). Axis: `kernel.marketplace_domain` + `kernel.events`.
- **`kernel/marketplace_store.py`** — `MarketplaceStore`: in-memory CRUD +
  SQLite (`packages` / `catalog` / `nodes` tables), mirroring `PlanStore` /
  `GraphStore` / `SwarmStore`.
- **Integration (backward-compatible, all default `None`):**
  - `AgentRuntime(marketplace=…)` + `install_capability(agent_id, package_id,
    capability_registry=…)` — installs a package and registers each declared
    capability in a `CapabilityRegistry`.
  - `WorkflowEngine(marketplace=…)` + `discover_plugins(capability_query)` —
    returns installed/available packages providing the queried capability.
- **No new dependency** — remote fetch via injected `http_client`; no `requests`/
  `aiohttp`.

## Consequences

- **+38 tests** (marketplace 12, integration 10, cluster 8, store 8), total
  **589 passed, 3 skipped**, kernel **92%**; `marketplace.py` 92%, `cluster.py`
  94%, `marketplace_store.py` 94%, `marketplace_domain.py` 100%; tach green.
- **Positive:** plugins can be discovered/installed/validated; optional multi-node
  membership + leader election + broadcast available.
- **Positive:** zero regression — all 551 pre-ADR-026 tests pass; marketplace is
  opt-in.
- **Negative:** the ADR-026 `NodeInfo`/`PluginPackage`/`CatalogEntry` models are
  in a *separate* module from ADR-023/ADR-007 (documented above) — a minor
  naming duplication by design.

## Honest Notes (known limitations)

- **Marketplace is local-logic only** — discovery uses an injected `http_client`;
  no real package signing/verification beyond a SHA-256 checksum when present.
- **Multi-node is logical/in-proc** — `broadcast`/`transport` are injected mocks;
  no TCP/gRPC transport yet (same constraint as ADR-023 swarm).
- **Leader election is oldest-node (bully-lite)**, not Raft/Paxos; no consensus
  for state mutation, so split-brain is possible on real partitions.
- **No automatic capability migration** on node failure — re-discovery required.
- **No sandboxing of installed plugins** — install validates metadata/checksum
  but does not isolate execution (that remains `kernel/sandbox.py`'s job).
