"""kernel — Hermes Kernel v2 core package (ADR-007 … ADR-025).

Public surface for the orchestration/distributed-health/semantic-memory layers.
"""

from kernel.domain import ExecutionOutcome, Plan, PlanStep, ReplanTrigger, Swarm, SwarmMember
from kernel.distributed_health import DistributedHealthMonitor
from kernel.dynamic_planner import DynamicPlanner
from kernel.graph_store import GraphStore
from kernel.knowledge_graph import KnowledgeGraphEngine
from kernel.plan_store import PlanStore
from kernel.semantic_graph import Entity, GraphQuery, InferenceRule, KnowledgeGraph, QueryResult, Relation
from kernel.swarm import SwarmCoordinator
from kernel.team_manager import TeamManager

__all__ = [
    "SwarmCoordinator",
    "TeamManager",
    "DistributedHealthMonitor",
    "Swarm",
    "SwarmMember",
    "DynamicPlanner",
    "Plan",
    "PlanStep",
    "ExecutionOutcome",
    "ReplanTrigger",
    "PlanStore",
    "KnowledgeGraphEngine",
    "KnowledgeGraph",
    "Entity",
    "Relation",
    "GraphQuery",
    "QueryResult",
    "InferenceRule",
    "GraphStore",
]
