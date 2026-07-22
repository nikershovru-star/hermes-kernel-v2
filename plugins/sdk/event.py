"""plugins/sdk/event.py — @on_event decorator (marks a method as an event handler)."""

from __future__ import annotations

from typing import Any, Callable

_EVENT_MARKER = "__sdk_event__"


def on_event(event_type: str) -> Callable:
    """Mark a class method to be subscribed to EventBus on `event_type`."""

    def decorate(func: Callable) -> Callable:
        setattr(func, _EVENT_MARKER, event_type)
        return func

    return decorate


def get_events(cls) -> list[tuple[str, Callable]]:
    """Harvest (event_type, method) pairs from a class (used by @agent)."""
    found: list[tuple[str, Callable]] = []
    for name, attr in vars(cls).items():
        etype = getattr(attr, _EVENT_MARKER, None)
        if etype is not None:
            found.append((etype, getattr(cls, name)))
    return found
