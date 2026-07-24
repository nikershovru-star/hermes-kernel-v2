"""kernel — Hermes Kernel v2 core package (ADR-007 … ADR-023).

Public surface for the orchestration/distributed-health layer added in
ADR-023 (Swarm / Teams).
"""

from kernel.domain import Swarm, SwarmMember
from kernel.distributed_health import DistributedHealthMonitor
from kernel.swarm import SwarmCoordinator
from kernel.team_manager import TeamManager

__all__ = [
    "SwarmCoordinator",
    "TeamManager",
    "DistributedHealthMonitor",
    "Swarm",
    "SwarmMember",
]
