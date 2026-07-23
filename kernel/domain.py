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


class Workflow(BaseEntity):
    name: str
    steps: list[str] = Field(default_factory=list)  # ordered entity ids
    status: str = "draft"  # draft|active|done


# --------------------------------------------------------------------------- #
# Organisation layer
# --------------------------------------------------------------------------- #
class Project(BaseEntity):
    name: str
    domain: str
    status: str = "active"


class Artifact(BaseEntity):
    type: str  # "note" | "diagram" | "report" | ...
    content: str
    source: Optional[str] = None


# --------------------------------------------------------------------------- #
# Plugin manifest (declarative plugin contract)
# --------------------------------------------------------------------------- #
class PluginManifest(BaseModel):
    """Declarative plugin contract (validated on construction)."""

    name: str  # also used as plugin_id (unique)
    version: str  # semver, e.g. "1.2.0"
    capabilities: list[str] = Field(default_factory=list)  # hermes.search, ...
    entrypoint: str  # import path: module:attr
    dependencies: list[str] = Field(default_factory=list)

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
