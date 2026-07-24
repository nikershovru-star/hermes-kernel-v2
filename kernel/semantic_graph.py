"""kernel/semantic_graph.py — Semantic-memory domain models (ADR-025).

Isolated from ``kernel.domain`` on purpose: ADR-004 already defines
``Entity`` / ``Relation`` (knowledge-pipeline node/edge types with a *different*
shape) in ``kernel.domain``. Redefining those names there would override the
ADR-004 models and regress the existing 504-test baseline. The ADR-025
semantic-memory layer therefore lives in its own axis-clean module and is
imported as ``from kernel.semantic_graph import Entity, Relation, ...``.

Axis contract: imports only ``pydantic`` (+ ``datetime``). The engine that uses
these models imports ``kernel.domain`` / ``kernel.events`` separately.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class EntityType(str, Enum):
    PERSON = "person"
    ORG = "org"
    CONCEPT = "concept"
    EVENT = "event"
    DOCUMENT = "document"
    CUSTOM = "custom"


class RelationType(str, Enum):
    KNOWS = "knows"
    PART_OF = "part_of"
    CAUSES = "causes"
    DEPENDS_ON = "depends_on"
    SIMILAR_TO = "similar_to"
    DERIVED_FROM = "derived_from"


class Entity(BaseModel):
    entity_id: str
    name: str
    type: EntityType
    properties: dict[str, Any] = Field(default_factory=dict)
    source: str = "unknown"
    confidence: float = 1.0
    created_at: datetime = Field(default_factory=_now)


class Relation(BaseModel):
    relation_id: str
    source_id: str
    target_id: str
    type: RelationType
    properties: dict[str, Any] = Field(default_factory=dict)
    weight: float = 1.0
    bidirectional: bool = False
    created_at: datetime = Field(default_factory=_now)


class KnowledgeGraph(BaseModel):
    graph_id: str
    name: str
    entities: dict[str, Entity] = Field(default_factory=dict)
    relations: dict[str, Relation] = Field(default_factory=dict)
    version: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class GraphQuery(BaseModel):
    query_id: str
    graph_id: str
    query_type: str  # entity_by_name | neighbors | path | similar | inference
    parameters: dict[str, Any] = Field(default_factory=dict)
    limit: int = 10


class InferenceRule(BaseModel):
    rule_id: str
    name: str
    pattern: dict[str, Any]
    action: str  # create_relation | merge_entities | raise_alert
    priority: int = 0
    enabled: bool = True


class QueryResult(BaseModel):
    result_id: str
    query_id: str
    entities: list[str] = Field(default_factory=list)
    relations: list[str] = Field(default_factory=list)
    inferred: list[str] = Field(default_factory=list)
    duration_ms: int = 0
