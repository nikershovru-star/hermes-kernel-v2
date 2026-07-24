"""kernel — Hermes Kernel v2 core package (ADR-007 … ADR-023).

Public surface for the orchestration/distributed-health layer added in
ADR-023 (Swarm / Teams).
"""

from kernel.domain import ExecutionOutcome, Plan, PlanStep, ReplanTrigger, Swarm, SwarmMember
from kernel.distributed_health import DistributedHealthMonitor
from kernel.dynamic_planner import DynamicPlanner
from kernel.plan_store import PlanStore
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
]
