"""kernel/domain.py — Pure typed domain entities for Hermes Kernel v2.

AXIS CONTRACT: this module MUST NOT import from kernel.bus, kernel.registry,
plugins.*, or mcp.*. It is the innermost layer — zero I/O, zero side effects.

All entities derive from `BaseEntity` which guarantees:
  - UUID string identifier (`id`)
  - `created_at` / `updated_at` ISO timestamps
  - integer `version` (for ADR-010 versioning)
  - free-form `metadata: dict[str, Any]` for extensibility

Design choice: Pydantic BaseModel (not frozen dataclass) because we need
serialization (JSON / Event Bus / MCP) and manifest validation out of the box.
Entities are immutable-by-convention (no public mutators); version bumps produce
new instances via `with_version()` helper (stub for ADR-010).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def _now() -> str:
    """UTC ISO-8601 timestamp with timezone, stable and sortable."""
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())


class BaseEntity(BaseModel):
    """Common contract for every domain entity."""

    id: str = Field(default_factory=_new_id)
    version: int = 1
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
    metadata: dict[str, Any] = Field(default_factory=dict)
    workspace_id: str = "default"  # every entity belongs to a workspace (ADR-011)

    model_config = {"extra": "allow", "populate_by_name": True}  # forward-compat; prefer explicit fields

    def with_version(self, version: int) -> "BaseEntity":
        """Return a copy with bumped version + refreshed updated_at (ADR-010 stub)."""
        clone = self.model_copy()
        clone.version = version
        clone.updated_at = _now()
        return clone


# --------------------------------------------------------------------------- #
# Knowledge / Content layer
# --------------------------------------------------------------------------- #
class Document(BaseEntity):
    source: str
    format: str  # "md" | "pdf" | "csv" | "api" | ... (ADR-009)
    content: str
    # embedding intentionally lives on Chunk, not Document


class Chunk(BaseEntity):
    document_id: str
    text: str
    embedding: Optional[list[float]] = None  # populated by embedder subsystem
    start: int = 0  # char offset in document
    end: int = 0

    @property
    def dim(self) -> int:
        return len(self.embedding) if self.embedding else 0


class KnowledgeNode(BaseEntity):
    label: str
    type: str
    domain: str  # isolates graphs per ADR-007
    properties: dict[str, Any] = Field(default_factory=dict)


class Entity(BaseEntity):
    name: str
    type: str
    domain: str
    aliases: list[str] = Field(default_factory=list)


class Relation(BaseEntity):
    source_id: str
    target_id: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Runtime / Orchestration layer
# --------------------------------------------------------------------------- #
class Task(BaseEntity):
    name: str
    capability: Optional[str] = None  # namespaced intent this task executes (Executor link)
    status: str = "PENDING"  # PENDING|QUEUED|RUNNING|COMPLETED|FAILED (Executor flow)
    priority: int = 5
    assigned_to: Optional[str] = None
    workflow_id: Optional[str] = None


class Event(BaseEntity):
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    source: str = "kernel"  # "agent:planner" | "plugin:obsidian" | "user" ...
    # timestamp наследуется от BaseEntity.created_at (single source of time)


class McpSessionEvent(BaseEntity):
    """One server→client SSE event logged for a Streamable HTTP session.

    Stored in ``PersistenceRegistry`` (workspace_id = ``mcp:<session_id>``) so a
    disconnected client can replay the backlog via ``Last-Event-ID`` (ADR-008
    resumability). ``seq`` is a per-session monotonic counter used as the
    SSE ``id`` and the replay boundary.
    """

    session_id: str
    seq: int
    sse_data: str  # fully-rendered SSE frame (already includes id:/data:)


class Memory(BaseEntity):
    type: str  # working|session|project|semantic|longterm (ADR-006)
    content: Any
    scope: str = "session"
    ttl: Optional[float] = None  # seconds; None = no expiry


class Tool(BaseEntity):
    name: str
    capability: str  # namespaced: hermes.search, hermes.graph.query ...
    input_schema: dict[str, Any] = Field(
        default_factory=dict, alias="schema"
    )  # JSON-schema of params; alias avoids shadowing pydantic BaseModel.schema


class Agent(BaseEntity):
    name: str
    capabilities: list[str] = Field(default_factory=list)
    status: str = "idle"  # idle|busy|offline


class WorkflowStatus(str, Enum):
    """Lifecycle states of a Workflow (ADR-019 state machine)."""

    DRAFT = "draft"
    PENDING = "pending"  # waiting for trigger
    RUNNING = "running"
    PAUSED = "paused"  # waiting for human approval
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"  # running compensation steps


class RetryPolicy(BaseModel):
    """Retry configuration for a workflow step (ADR-019)."""

    max_attempts: int = 3
    backoff_seconds: float = 1.0
    exponential: bool = True


class WorkflowTrigger(BaseModel):
    """What starts a workflow: manual, event, schedule, or webhook (ADR-019)."""

    type: str = "manual"  # manual | event | schedule | webhook
    config: dict[str, Any] = Field(default_factory=dict)


class WorkflowStep(BaseModel):
    """One node in a Workflow DAG (ADR-019).

    ``input_mapping`` references previous steps' outputs via dotted paths,
    e.g. ``{"x": "step_1.output.bbox.x"}`` (resolved by the engine).
    """

    id: str
    name: str
    capability: str  # "desktop.screenshot", "desktop.ocr", "agent.reason" ...
    input_mapping: dict[str, str] = Field(default_factory=dict)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    compensation: str | None = None  # step_id to run on failure
    requires_approval: bool = False
    timeout_seconds: float = 30.0


class Workflow(BaseEntity):
    """A declarative, executable workflow DAG (ADR-019).

    Replaces the earlier primitive ``Workflow`` (name + list[str] step ids).
    A workflow is a DAG of ``WorkflowStep`` nodes with explicit transitions,
    retry policies, compensation, and optional human-approval gates — not a
    linear script.
    """

    name: str
    description: str = ""
    steps: list[WorkflowStep] = Field(default_factory=list)
    status: WorkflowStatus = WorkflowStatus.DRAFT
    trigger: WorkflowTrigger | None = None
    context: dict[str, Any] = Field(default_factory=dict)  # shared workflow context


class WorkflowInstance(BaseEntity):
    """A running instance of a Workflow — state-machine snapshot (ADR-019)."""

    workflow_id: str
    status: WorkflowStatus
    current_step_id: str | None = None
    step_results: dict[str, Any] = Field(default_factory=dict)  # step_id -> artifact id/content
    step_attempts: dict[str, int] = Field(default_factory=dict)
    event_log: list[str] = Field(default_factory=list)  # DomainEvent ids
    started_at: datetime | None = None
    completed_at: datetime | None = None


# --------------------------------------------------------------------------- #
# Organisation layer
# --------------------------------------------------------------------------- #
class Project(BaseEntity):
    name: str
    domain: str
    status: str = "active"


class Artifact(BaseEntity):
    """Unified result object returned by capabilities / agents (ADR-016).

    Replaces ad-hoc string/bytes payloads with a versioned, linkable,
    provenance-carrying entity so a caller can answer "where is the screenshot
    I took yesterday?" via workspace-scoped persistence + ``provenance``.
    """

    type: str  # "screenshot" | "code" | "text" | "dataset" | "note" | "report" ...
    content: Any  # decoded payload (str, bytes, dict, ...)
    format: str = "text"  # "png" | "py" | "md" | "json" | "base64" ...
    source: Optional[str] = None  # "agent:browser" | "plugin:desktop" | ...
    provenance: list[str] = Field(default_factory=list)  # ordered chain of action ids


# --------------------------------------------------------------------------- #
# Plugin manifest (declarative plugin contract)
# --------------------------------------------------------------------------- #
class SandboxPolicy(BaseModel):
    """Resource + security policy for a plugin/agent/workflow (ADR-020).

    Declared in ``plugin.yaml`` / agent manifest / workflow context; enforced
    best-effort by ``kernel.sandbox.Sandbox``. Defaults are intentionally
    permissive so the kernel runs unchanged on first adoption.
    """

    max_cpu_time_ms: int = 30_000
    max_memory_mb: int = 512
    max_file_size_mb: int = 100
    max_files_open: int = 64
    allow_network: bool = True
    allow_subprocess: bool = False
    timeout_seconds: float = 30.0
    max_retries: int = 0  # sandbox-level retry (distinct from workflow retry)

    def serialized(self) -> dict[str, Any]:
        return self.model_dump()


class SandboxViolation(BaseModel):
    """Recorded when a ``SandboxPolicy`` is breached (ADR-020)."""

    policy: SandboxPolicy
    violation_type: str  # "timeout" | "memory" | "cpu" | "file" | "network" | "subprocess"
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
# Health & Recovery (ADR-021)
# --------------------------------------------------------------------------- #
class HealthCheck(BaseModel):
    """Probe configuration for a component (ADR-021).

    Declared in agent manifest / workflow context; enforced by HealthMonitor.
    """

    probe_type: str = "liveness"  # liveness | readiness | startup
    interval_seconds: float = 10.0
    timeout_seconds: float = 5.0
    failure_threshold: int = 3  # consecutive failures → unhealthy
    success_threshold: int = 1  # consecutive successes → healthy
    enabled: bool = True


class HealthStatus(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"  # some probes failing, not yet critical
    UNHEALTHY = "unhealthy"  # failure_threshold breached
    UNKNOWN = "unknown"  # no probes yet


class HealthRecord(BaseModel):
    """Snapshot of a component's health at a point in time (ADR-021)."""

    component_id: str  # agent_id / workflow_instance_id / plugin_id
    component_type: str  # "agent" | "workflow" | "plugin"
    status: HealthStatus = HealthStatus.UNKNOWN
    last_probe_at: datetime | None = None
    consecutive_failures: int = 0
    consecutive_successes: int = 0
    last_error: str | None = None


class DeadLetterEntry(BaseModel):
    """A failed task/event stored for replay/analysis (ADR-021)."""

    entry_id: str
    component_id: str
    entry_type: str  # "task" | "event" | "workflow_step"
    payload: dict[str, Any]  # serialized task / event / step
    error: str  # failure reason
    sandbox_violation: SandboxViolation | None = None
    retry_count: int = 0
    max_retries: int = 3
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    recovered_at: datetime | None = None


class CircuitBreakerPolicy(BaseModel):
    """Circuit breaker configuration (ADR-021)."""

    failure_threshold: int = 5  # failures before OPEN
    recovery_timeout_seconds: float = 60.0  # HALF-OPEN wait
    success_threshold: int = 2  # successes to CLOSE from HALF-OPEN


class CircuitBreakerState(str, Enum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast, rejecting calls
    HALF_OPEN = "half_open"  # testing if recovered


# --------------------------------------------------------------------------- #
# Behavior Engine (ADR-022) — human-like desktop automation config
# --------------------------------------------------------------------------- #
class BehaviorProfile(BaseModel):
    """Human-like behavior configuration (ADR-022).

    Tuning knobs for how "human" the automation appears. Defaults model an
    average, unhurried user. All ``(min, max)`` ranges are inclusive bounds a
    behavior engine samples uniformly.
    """

    # Mouse
    mouse_speed: float = 1.0  # 0.5=slow, 1.0=normal, 2.0=fast
    mouse_curve: str = "bezier"  # "linear" | "bezier" | "catmull"
    mouse_overshoot: bool = True  # slight overshoot + correction
    mouse_pause_ms: tuple[int, int] = (50, 150)  # pause range after move

    # Scroll
    scroll_momentum: bool = True  # accelerate → coast → decelerate
    scroll_distance_px: tuple[int, int] = (300, 800)  # per-scroll distance
    scroll_pause_ms: tuple[int, int] = (500, 2000)  # pause between scrolls
    scroll_reading_pause_ms: tuple[int, int] = (2000, 5000)  # pause at content

    # Typing
    typing_wpm: int = 40  # words per minute target
    typing_error_rate: float = 0.02  # 2% typo + backspace
    typing_burst_size: tuple[int, int] = (3, 8)  # chars typed before pause

    # Gaze / Reading
    gaze_fixation_ms: tuple[int, int] = (150, 400)  # look before click
    gaze_saccade_ms: tuple[int, int] = (20, 80)  # move between elements
    reading_words_per_fixation: int = 2  # words per gaze stop
    reading_regression_rate: float = 0.1  # 10% backward saccades


class BehaviorSession(BaseModel):
    """A running behavior session with mutable state (ADR-022)."""

    profile: BehaviorProfile
    current_position: tuple[int, int] = (0, 0)  # last mouse position
    scroll_position: int = 0  # current scroll Y
    gaze_target: tuple[int, int] | None = None  # where "eyes" are looking
    action_log: list[str] = Field(default_factory=list)  # behavior event ids


class HumanBehaviorProfile(BaseModel):
    """Persistent human behavior profile (ADR-022).

    Saved per-user, loaded at session start. Extends ``BehaviorProfile`` with
    identity-specific metadata (name, timestamps). Named ``HumanBehaviorProfile``
    to avoid colliding with the ADR-013 ``HumanProfile`` (Human Emulation layer).
    """

    profile_id: str
    name: str
    behavior: BehaviorProfile = Field(default_factory=BehaviorProfile)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# --------------------------------------------------------------------------- #
# Swarm / Teams (ADR-023) — multi-agent orchestration + distributed health
# --------------------------------------------------------------------------- #
class SwarmTopology(str, Enum):
    LEADER_WORKER = "leader_worker"
    MESH = "mesh"


class SwarmMember(BaseModel):
    """One agent participating in a swarm (ADR-023)."""

    agent_id: str
    node_id: str
    role: str = "worker"  # "leader" | "worker" | "observer"
    health: str = "healthy"  # "healthy" | "suspected" | "unhealthy"
    last_heartbeat: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    capabilities: list[str] = Field(default_factory=list)


class Swarm(BaseModel):
    """A group of agents cooperating under a topology (ADR-023)."""

    swarm_id: str
    topology: SwarmTopology = SwarmTopology.LEADER_WORKER
    members: dict[str, SwarmMember] = Field(default_factory=dict)  # agent_id -> member
    leader_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NodeInfo(BaseModel):
    """A logical node in the (in-proc) distributed topology (ADR-023)."""

    node_id: str
    address: str = "inproc"  # logical address; real TCP/gRPC is future work
    capabilities: list[str] = Field(default_factory=list)
    load_score: float = 0.0  # 0.0 (idle) .. 1.0 (saturated)
    last_seen: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class TaskDelegation(BaseModel):
    """Record of a task handed from one agent to another (ADR-023)."""

    delegation_id: str
    task_id: str
    from_agent: str
    to_agent: str
    swarm_id: str
    status: str = "pending"  # "pending" | "running" | "completed" | "failed"
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PluginManifest(BaseModel):
    """Declarative plugin contract (validated on construction)."""

    name: str  # also used as plugin_id (unique)
    version: str  # semver, e.g. "1.2.0"
    capabilities: list[str] = Field(default_factory=list)  # hermes.search, ...
    entrypoint: str  # import path: module:attr
    dependencies: list[str] = Field(default_factory=list)
    sandbox_policy: dict[str, Any] | None = None  # optional SandboxPolicy as dict (ADR-020)

    @property
    def plugin_id(self) -> str:
        return self.name


# --------------------------------------------------------------------------- #
# Capability / Workspace / Dataset / Conversation (P1)
# --------------------------------------------------------------------------- #
class Capability(BaseEntity):
    """Declarative capability grouping tools behind a namespaced intent."""

    name: str  # hermes.search, hermes.graph.query ...
    description: str = ""
    tools: list[str] = Field(default_factory=list)  # Tool.names bundled by this capability
    input_schema: dict[str, Any] = Field(default_factory=dict, alias="schema")  # capability-level input schema


class Workspace(BaseEntity):
    """Isolation boundary (ADR-011): Personal / Project / Team."""

    name: str
    owner_id: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)


class Dataset(BaseEntity):
    """Raw data container (ADR-009 placeholder)."""

    name: str
    source: str = ""
    format: str = "unknown"  # md|pdf|csv|api ...


class Conversation(BaseEntity):
    """Chat transcript (ADR-003 placeholder)."""

    title: str = ""
    messages: list[dict[str, Any]] = Field(default_factory=list)


class User(BaseEntity):
    """Authenticated principal (P5 multi-tenancy)."""

    username: str
    hashed_password: str = ""  # pbkdf2 "salt$hash" (never store plaintext)
    roles: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------- #
# Human Emulation Layer (ADR-013)
# --------------------------------------------------------------------------- #
class HumanProfile(BaseEntity):
    """Digital-twin profile describing a human's behaviour for emulation."""

    name: str
    typing_speed_wpm: int = 60
    typo_rate: float = 0.02
    pause_between_actions: tuple[float, float] = (0.5, 2.0)
    preferred_browser: str = "chromium"
    screen_resolution: tuple[int, int] = (1920, 1080)
    user_agent: str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


class BrowserSession(BaseEntity):
    """One browser session (tab/window) owned by a HumanProfile."""

    profile_id: str  # FK to HumanProfile.id
    url: str
    status: str = "idle"  # idle | loading | interacting | closed
    screenshot_path: Optional[str] = None
    last_action: Optional[str] = None


class ActionLog(BaseEntity):
    """Audit trail of every emulated action (accountability)."""

    session_id: str
    action_type: str  # navigate | click | type | screenshot | move | key
    target: Optional[str] = None  # selector, coordinates, etc.
    payload: dict[str, Any] = Field(default_factory=dict)
    success: bool = True
    error: Optional[str] = None


# Convenience registry for tests / loaders
ENTITY_TYPES = {
    "Document": Document,
    "Chunk": Chunk,
    "KnowledgeNode": KnowledgeNode,
    "Entity": Entity,
    "Relation": Relation,
    "Task": Task,
    "Event": Event,
    "Memory": Memory,
    "Tool": Tool,
    "Agent": Agent,
    "Workflow": Workflow,
    "WorkflowInstance": WorkflowInstance,
    "Project": Project,
    "Artifact": Artifact,
    "Capability": Capability,
    "Workspace": Workspace,
    "Dataset": Dataset,
    "Conversation": Conversation,
    "User": User,
    "HumanProfile": HumanProfile,
    "BrowserSession": BrowserSession,
    "ActionLog": ActionLog,
}
