# ADR-023 — Swarm / Teams (Multi-Agent Orchestration & Distributed Health)

- **Status:** Accepted
- **Date:** 2026-07-24
- **Deciders:** Hermes Kernel v2 architecture review (v2.9.0)
- **Depends on:** ADR-017 (Event Platform), ADR-021 (Health & Recovery), ADR-022 (Behavior Engine)

---

## Context

The v5 Capability Platform needs coordinated multi-agent execution: one task
farmed across several agents, a leader to arbitrate, and health awareness so a
dead agent is detected and its work re-routed. Until now every agent ran
singly — `AgentRuntime` tracked one process's agents, with no notion of a group,
leadership, or cross-agent task delegation. Four gaps motivated this release:

1. **No team abstraction** — no way to group agents into a named "team"/swarm.
2. **No leadership** — no deterministic, reproducible leader election.
3. **No distributed health** — a stalled agent on another "node" was invisible.
4. **No delegation** — a capability missing locally could not be handed to a peer.

## Decision

- **`kernel/domain.py`** — `SwarmTopology` (LEADER_WORKER | MESH), `SwarmMember`
  (agent_id, node_id, role, health, last_heartbeat, capabilities), `Swarm`
  (members dict + leader_id), `NodeInfo`, `TaskDelegation`.
- **`kernel/events.py`** — 8 swarm events: `AgentJoinedSwarm`, `AgentLeftSwarm`,
  `HeartbeatReceived`, `HeartbeatMissed`, `LeaderElected`, `TaskDelegated`,
  `TaskCompleted`, `NodePartitioned` (using the `super().__init__(type=...)` convention).
- **`kernel/swarm.py`** — `SwarmCoordinator`:
  - *Membership* — create/join/leave; LEADER_WORKER auto-promotes the first
    joiner to leader; leader departure triggers re-election.
  - *Election* — **Bully** algorithm: among healthy members, the highest
    lexicographic `agent_id` becomes leader. `LeaderElected` emitted.
  - *Heartbeat / health* — `handle_heartbeat` updates liveness + load;
    `check_partitions` marks `suspected` past `suspicion_timeout_ms` (3s) and
    `unhealthy` past `failure_timeout_ms` (10s), emits `HeartbeatMissed` /
    `NodePartitioned`, and re-elects if the leader is lost.
  - *Delegation* — `delegate_task` selects an eligible member (capability-aware,
    skips `suspected`/`unhealthy`), preferring lowest load, with round-robin
    tie-break; emits `TaskDelegated`.
  - *Determinism* — injectable `clock` (monotonic seconds), `sleep` (async stub),
    `rng`, `event_bus`, `event_store`, `health_monitor`, timeouts.
- **`kernel/swarm_store.py`** — `SwarmStore`: in-memory CRUD + optional SQLite
  (`swarms`, `delegations` tables), mirroring the ADR-022 `HumanProfileStore`.
- **`kernel/distributed_health.py`** — `DistributedHealthMonitor`: emits
  `HeartbeatReceived` on an interval, tracks remote node health via the
  injectable clock; standalone but integrates with the coordinator.
- **`kernel/team_manager.py`** — `TeamManager`: high-level facade — `create_team`,
  `assign_role`, `disband_team`, `execute_distributed` (delegates a batch of
  tasks across swarm members, optional injected `executor`).
- **Optional integration (backward-compatible):**
  - `AgentRuntime(swarm_coordinator=…)` + `join_swarm`/`leave_swarm`; `execute`
    delegates when the capability is missing locally.
  - `WorkflowEngine(swarm_coordinator=…)` + `schedule_swarm` / `execute_step_swarm`.
  - `CapabilityExecutor.discover_remote(swarm_id, coordinator)` — unique healthy
    member capabilities.
- **No new dependency** — pure asyncio + existing kernel infrastructure.

## Consequences

- **+49 tests** (swarm_coordinator 14 + extra 7, distributed_health 8,
  team_manager 8, swarm_integration 8, swarm_store 4) — total **461 passed,
  3 skipped**; kernel coverage **91%**; tach green.
- Single-agent mode is **unchanged** (all new params default `None`).

### Honest notes (deferred)

- **Bully, not Raft/Paxos** — simple, deterministic, but not partition-resilient
  across *real* separate nodes (only logical in-proc nodes here).
- **Timeout suspicion, not Phi-accrual** — coarse binary thresholds.
- **Logical multi-node only** — "distributed" means in-proc EventBus routing;
  no TCP/gRPC transport yet.
- **No consensus for state mutation** — EventBus eventual consistency; split-brain
  possible if a partition heals without explicit reconciliation.
- **Round-robin + capability filter**, not weighted least-connections.
- **No automatic task migration** on worker failure — the delegator must
  retry/re-schedule.
- **Swarm state persistence is local SQLite only** (no cloud sync).
- `HeartbeatMissed` / `NodePartitioned` are keyed on `node_id` (not `swarm_id`),
  so subscribers/replay must query by node aggregate.
