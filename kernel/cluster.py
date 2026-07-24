"""kernel/cluster.py — ClusterManager (ADR-026, multi-node).

Logical multi-node coordination: membership, leader election and broadcast.
Transport is injected (``transport.send(node_id, message)``) so no real
network is required; "distributed" here means in-process routing only.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from kernel.events import EventBus, EventStore, NodeJoined, NodeLeft
from kernel.marketplace_domain import ClusterTopology, NodeInfo

logger = logging.getLogger("hermes.kernel.cluster")


class ClusterManager:
    """Track cluster membership, elect a leader, broadcast messages.

    AXIS CONTRACT: imports only ``kernel.marketplace_domain`` + ``kernel.events``.
    """

    def __init__(
        self,
        cluster_id: str = "default",
        event_bus: EventBus | None = None,
        event_store: EventStore | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        transport: Any | None = None,
        node_timeout: float = 30.0,
    ) -> None:
        self._cluster_id = cluster_id
        self._bus = event_bus
        self._event_store = event_store
        self._clock = clock
        self._transport = transport
        self._node_timeout = node_timeout
        self._nodes: dict[str, NodeInfo] = {}

    # -- membership ------------------------------------------------------ #
    async def join_cluster(self, node_id: str, address: str, capabilities: list[str]) -> NodeInfo:
        node = NodeInfo(
            node_id=node_id,
            address=address,
            capabilities=list(capabilities),
            last_heartbeat=self._clock(),
            load=0.0,
        )
        self._nodes[node_id] = node
        # re-elect if no leader yet
        topo = self.get_topology()
        if topo.leader_id is None:
            self.elect_leader()
        await self._emit(NodeJoined(node_id, address, self._cluster_id, capabilities))
        return node

    async def leave_cluster(self, node_id: str, reason: str = "") -> bool:
        node = self._nodes.pop(node_id, None)
        if node is None:
            return False
        topo = self.get_topology()
        if topo.leader_id == node_id:
            self.elect_leader()
        await self._emit(NodeLeft(node_id, self._cluster_id, reason))
        return True

    def get_topology(self) -> ClusterTopology:
        return ClusterTopology(
            cluster_id=self._cluster_id,
            nodes=dict(self._nodes),
            leader_id=self._leader_id(),
        )

    # -- leader election ------------------------------------------------- #
    def elect_leader(self) -> str | None:
        """Elect the oldest node (earliest ``last_heartbeat``) as leader."""
        if not self._nodes:
            self._set_leader(None)
            return None
        leader = min(self._nodes.values(), key=lambda n: n.last_heartbeat)
        self._set_leader(leader.node_id)
        return leader.node_id

    def _leader_id(self) -> str | None:
        # derive from a stored attribute if set, else compute
        return getattr(self, "_leader", None)

    def _set_leader(self, node_id: str | None) -> None:
        self._leader = node_id

    # -- heartbeat / timeout -------------------------------------------- #
    def heartbeat(self, node_id: str) -> None:
        if node_id in self._nodes:
            self._nodes[node_id].last_heartbeat = self._clock()

    def prune_timed_out(self) -> list[str]:
        """Remove nodes whose last_heartbeat is older than ``node_timeout`` (seconds)."""
        now = self._clock()
        dropped: list[str] = []
        for nid, n in list(self._nodes.items()):
            age = (now - n.last_heartbeat).total_seconds()
            if age > self._node_timeout:
                dropped.append(nid)
                self._nodes.pop(nid, None)
        if dropped and self._leader_id() in dropped:
            self.elect_leader()
        return dropped

    # -- broadcast ------------------------------------------------------- #
    async def broadcast(self, message: Any) -> list[str]:
        """Send ``message`` to all live nodes via the injected transport.

        Returns the list of node_ids the message was delivered to.
        """
        delivered: list[str] = []
        for nid in self._nodes:
            if self._transport is not None:
                send = self._transport.send(nid, message)
                if hasattr(send, "__await__"):
                    await send
            delivered.append(nid)
        return delivered

    # -- helpers --------------------------------------------------------- #
    async def _emit(self, event: Any) -> None:
        if self._bus is not None:
            self._bus.publish(event)
        if self._event_store is not None:
            try:
                await self._event_store.append(event)
            except Exception:  # noqa: BLE001
                pass
