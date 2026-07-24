"""tests/test_dead_letter.py — DeadLetterQueue append/list/recover/replay (ADR-021).

Covers: append + event emission, list by component + limit, recover marks
recovered_at + emits event, replay is idempotent (only unrecovered replayed),
and handler-decides semantics.
"""

from __future__ import annotations

import uuid

from kernel.bus import EventBus
from kernel.domain import DeadLetterEntry
from kernel.events import EventStore
from kernel.health import DeadLetterQueue


def _entry(component_id: str = "a1", entry_type: str = "task") -> DeadLetterEntry:
    return DeadLetterEntry(
        entry_id=str(uuid.uuid4()),
        component_id=component_id,
        entry_type=entry_type,
        payload={"x": 1},
        error="boom",
    )


async def test_append_and_count() -> None:
    dlq = DeadLetterQueue()
    await dlq.append(_entry())
    await dlq.append(_entry())
    assert dlq.count() == 2


async def test_append_emits_event() -> None:
    store = EventStore()
    dlq = DeadLetterQueue(event_store=store, event_bus=EventBus())
    e = _entry()
    await dlq.append(e)
    events = await store.read_stream(e.component_id)
    assert any(ev.type == "health.dead_letter_appended" for ev in events)


async def test_list_by_component_and_limit() -> None:
    dlq = DeadLetterQueue()
    for _ in range(3):
        await dlq.append(_entry("a1"))
    await dlq.append(_entry("b2"))
    assert len(await dlq.list("a1")) == 3
    assert len(await dlq.list("b2")) == 1
    assert len(await dlq.list(limit=2)) == 2
    assert dlq.count("a1") == 3


async def test_recover_marks_and_emits() -> None:
    store = EventStore()
    dlq = DeadLetterQueue(event_store=store, event_bus=EventBus())
    e = _entry()
    await dlq.append(e)
    recovered = await dlq.recover(e.entry_id)
    assert recovered is not None
    assert recovered.recovered_at is not None
    events = await store.read_stream(e.component_id)
    assert any(ev.type == "health.dead_letter_recovered" for ev in events)


async def test_recover_unknown_returns_none() -> None:
    dlq = DeadLetterQueue()
    assert await dlq.recover("nope") is None


async def test_replay_only_unrecovered_and_idempotent() -> None:
    dlq = DeadLetterQueue()
    e1 = _entry("a1")
    e2 = _entry("a1")
    await dlq.append(e1)
    await dlq.append(e2)
    await dlq.recover(e1.entry_id)  # already recovered → skipped by replay

    seen: list[str] = []

    async def handler(entry: DeadLetterEntry) -> bool:
        seen.append(entry.entry_id)
        return True

    count = await dlq.replay("a1", handler)
    assert count == 1
    assert seen == [e2.entry_id]

    # Second replay does nothing (idempotent — all recovered now).
    count2 = await dlq.replay("a1", handler)
    assert count2 == 0


async def test_replay_handler_failure_keeps_entry() -> None:
    dlq = DeadLetterQueue()
    e = _entry("a1")
    await dlq.append(e)

    async def failing(_entry: DeadLetterEntry) -> bool:
        return False

    count = await dlq.replay("a1", failing)
    assert count == 0
    assert dlq.get(e.entry_id).recovered_at is None
