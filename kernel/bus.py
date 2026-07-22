"""kernel/bus.py — Async, in-memory EventBus for Hermes Kernel v2.

AXIS CONTRACT: imports only from kernel.domain. No I/O, no persistence yet
(persistence arrives with ADR-009/Data-Lake). This is the transport layer
mandated by ADR-001 — all inter-subsystem communication flows through here.

Semantics
---------
- `publish` is fire-and-forget: it schedules delivery and returns immediately.
- Delivery is **at-least-once** (in-memory; a crash loses in-flight events).
- A handler exception is logged and isolated; other subscribers still receive
  the event (fault containment, no cascade failure).
- `wait_for` returns a Future that resolves with the first matching Event,
  or raises asyncio.TimeoutError on expiry — a *sync barrier* for critical paths.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import Awaitable, Callable

from kernel.domain import Event

logger = logging.getLogger("hermes.kernel.bus")

# Strict, mypy-friendly aliases
Handler = Callable[["Event"], Awaitable[None]]
SubscriptionId = str


class EventBus:
    """Async in-memory publish/subscribe bus with a sync-barrier wait_for."""

    def __init__(self) -> None:
        # event_type -> {subscription_id: handler}
        self._subs: dict[str, dict[SubscriptionId, Handler]] = defaultdict(dict)
        # Futures waiting on a set of event types (sync barriers)
        self._waiters: list[tuple[frozenset[str], asyncio.Future[Event]]] = []

    # -- subscription ----------------------------------------------------- #
    def subscribe(self, event_type: str, handler: Handler) -> SubscriptionId:
        """Register `handler` for `event_type`. Returns a subscription id."""
        sub_id = str(uuid.uuid4())
        self._subs[event_type][sub_id] = handler
        logger.debug("subscribed %s -> %s", sub_id, event_type)
        return sub_id

    def unsubscribe(self, subscription_id: str) -> bool:
        """Remove a subscription by id. Returns True if it existed."""
        for handlers in self._subs.values():
            if handlers.pop(subscription_id, None) is not None:
                logger.debug("unsubscribed %s", subscription_id)
                return True
        return False

    # -- publish ---------------------------------------------------------- #
    def publish(self, event: Event) -> None:
        """Fire-and-forget. Schedules async delivery to all subscribers."""
        if not isinstance(event, Event):
            raise TypeError(f"publish expects Event, got {type(event).__name__}")
        asyncio.create_task(self._dispatch(event))

    async def _dispatch(self, event: Event) -> None:
        """Deliver `event` to subscribers (at-least-once) + resolve waiters."""
        handlers = list(self._subs.get(event.type, {}).values())
        for handler in handlers:
            try:
                await handler(event)
            except Exception:  # fault containment: never break other subscribers
                logger.exception("handler failed for event %s", event.type)

        # Resolve any waiting sync-barriers
        still_waiting: list[tuple[frozenset[str], asyncio.Future[Event]]] = []
        for types, fut in self._waiters:
            if fut.done():
                continue
            if event.type in types:
                fut.set_result(event)
            else:
                still_waiting.append((types, fut))
        self._waiters = still_waiting

    # -- sync barrier ----------------------------------------------------- #
    def wait_for(
        self, event_types: list[str], timeout: float = 30.0
    ) -> "asyncio.Future[Event]":
        """Return a Future resolving with the first matching Event.

        Raises asyncio.TimeoutError on expiry (future rejected).
        """
        types = frozenset(event_types)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Event] = loop.create_future()

        def _on_timeout() -> None:
            if not fut.done():
                fut.set_exception(asyncio.TimeoutError(event_types))

        loop.call_later(timeout, _on_timeout)
        self._waiters.append((types, fut))
        return fut

    # -- introspection ---------------------------------------------------- #
    def subscriber_count(self, event_type: str) -> int:
        return len(self._subs.get(event_type, {}))

    def clear(self) -> None:
        """Drop all subscriptions and waiters (test isolation)."""
        self._subs.clear()
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()

    async def close(self) -> None:
        """Graceful shutdown: cancel pending waiters, clear subscriptions."""
        for _, fut in self._waiters:
            if not fut.done():
                fut.cancel()
        self._waiters.clear()
        self._subs.clear()
