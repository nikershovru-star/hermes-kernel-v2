"""kernel/distributed_health.py — DistributedHealthMonitor (ADR-023).

AXIS CONTRACT: depends only on ``kernel.domain`` + ``kernel.events`` + stdlib.
Never imports plugins/ or mcp/.

Extends the ADR-021 health concept to multi-agent: periodic heartbeats emitted
onto the EventBus, per-node health tracking, and injectable clock/sleep for
deterministic tests. Integrates cleanly with ``SwarmCoordinator`` (the coordinator
can consume the heartbeats via ``handle_heartbeat``), but is usable standalone.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from kernel.domain import NodeInfo
from kernel.events import HeartbeatReceived


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DistributedHealthMonitor:
    """Emits heartbeats for local node(s) and tracks remote node health.

    Injectables (all optional):
      - ``clock``: ``() -> float`` monotonic seconds (deterministic timeouts).
      - ``sleep``: ``async (float) -> None`` stub (no real delay in tests).
    """

    def __init__(
        self,
        event_bus: Optional[object] = None,
        event_store: Optional[object] = None,
        node_id: str = "local",
        interval_seconds: float = 1.0,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._bus = event_bus
        self._ev_store = event_store
        self._node_id = node_id
        self._interval = interval_seconds
        self._clock = clock or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._nodes: dict[str, NodeInfo] = {}
        self._last_seen_ts: dict[str, float] = {}  # node_id -> monotonic secs
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._beat_count = 0

    # -- tracking --------------------------------------------------------- #
    def track_node(self, node_id: str, capabilities: Optional[list[str]] = None) -> NodeInfo:
        info = NodeInfo(node_id=node_id, capabilities=list(capabilities or []), last_seen=_now())
        self._nodes[node_id] = info
        self._last_seen_ts[node_id] = self._clock()
        return info

    def get_node_health(self, node_id: str) -> str:
        if node_id not in self._nodes:
            return "unknown"
        age = (self._clock() - self._last_seen_ts.get(node_id, 0.0)) * 1000.0
        if age > 10000:
            return "unhealthy"
        if age > 3000:
            return "suspected"
        return "healthy"

    def record_heartbeat(self, node_id: str, load_score: float = 0.0) -> None:
        info = self._nodes.get(node_id)
        if info is None:
            info = self.track_node(node_id)
        info.last_seen = _now()
        info.load_score = load_score
        self._last_seen_ts[node_id] = self._clock()

    # -- heartbeat emission ---------------------------------------------- #
    async def send_heartbeat(self, load_score: float = 0.0) -> None:
        self._beat_count += 1
        ts = self._clock()
        self.record_heartbeat(self._node_id, load_score)
        event = HeartbeatReceived(self._node_id, self._node_id, ts, load_score)
        if self._ev_store is not None:
            await self._ev_store.append(event)  # type: ignore[attr-defined]
        if self._bus is not None:
            self._bus.publish(event)  # type: ignore[attr-defined]

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.ensure_future(self._beat_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _beat_loop(self) -> None:
        while self._running:
            try:
                await self._sleep(self._interval)
            except asyncio.CancelledError:
                break
            await self.send_heartbeat()

    @property
    def beat_count(self) -> int:
        return self._beat_count


__all__ = ["DistributedHealthMonitor"]
