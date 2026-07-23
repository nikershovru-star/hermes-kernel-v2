"""tests/test_event_platform.py — Event Platform foundation (ADR-017).

Covers: DomainEvent (extends Event, flows via EventBus), EventStore
append-only, CommandBus dispatch (command -> handler -> events), CQRS
ReadModel projections + QueryBus roundtrip. Vision-free; no optional deps.
"""

from __future__ import annotations

import asyncio
import pytest
from datetime import datetime, timezone

from kernel.bus import EventBus
from kernel.domain import Agent, Task
from kernel.events import (
    Command,
    CommandBus,
    DomainEvent,
    EventStore,
    Query,
    QueryBus,
    ReadModel,
)


# -- read models (projections) ------------------------------------------- #
async def _ret(v):
    return v


class AgentStatusReadModel(ReadModel):
    def __init__(self) -> None:
        self.state: dict[str, dict] = {}

    async def handle(self, event: DomainEvent) -> None:
        if event.type == "agent.started":
            self.state[event.aggregate_id] = {"state": "running", "last_seen": event.timestamp}
        elif event.type == "agent.stopped":
            if event.aggregate_id in self.state:
                self.state[event.aggregate_id]["state"] = "stopped"

    def reset(self) -> None:
        self.state.clear()


class CapabilityMetricsReadModel(ReadModel):
    def __init__(self) -> None:
        self.metrics: dict[str, dict] = {}

    async def handle(self, event: DomainEvent) -> None:
        if event.type == "capability.executed":
            cap = event.aggregate_id
            m = self.metrics.setdefault(cap, {"count": 0, "total_ms": 0.0})
            m["count"] += 1
            m["total_ms"] += event.payload.get("duration_ms", 0.0)

    def reset(self) -> None:
        self.metrics.clear()


# -- commands + queries -------------------------------------------------- #
class StartAgentCmd(Command):
    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id)


class StopAgentCmd(Command):
    def __init__(self, agent_id: str) -> None:
        super().__init__(agent_id)


class GetAgentStatusQuery(Query):
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id


@pytest.mark.asyncio
async def test_domain_event_extends_event_and_bus_accepts() -> None:
    bus = EventBus()
    received: list[DomainEvent] = []

    async def handler(e):
        received.append(e)

    bus.subscribe("agent.started", handler)
    ev = DomainEvent(type="agent.started", aggregate_id="a1", payload={"x": 1})
    # EventBus.publish requires Event; DomainEvent IS-A Event
    bus.publish(ev)
    await asyncio.sleep(0.01)
    assert received and received[0].id == ev.id
    assert received[0].aggregate_id == "a1"


@pytest.mark.asyncio
async def test_event_store_append_only_and_read_stream() -> None:
    store = EventStore()
    await store.append(DomainEvent(type="desktop.clicked", aggregate_id="a1", payload={"x": 1}))
    await store.append(DomainEvent(type="desktop.clicked", aggregate_id="a1", payload={"x": 2}))
    await store.append(DomainEvent(type="desktop.clicked", aggregate_id="a2", payload={"x": 3}))
    stream = await store.read_stream("a1")
    assert len(stream) == 2
    assert all(e.aggregate_id == "a1" for e in stream)
    # no mutation API exists; append-only invariant is structural
    assert store.count() == 3


@pytest.mark.asyncio
async def test_command_bus_dispatches_and_emits_event() -> None:
    bus = EventBus()
    store = EventStore()
    cbus = CommandBus(bus, store)
    emitted: list[DomainEvent] = []
    async def _on_ev(e):
        emitted.append(e)

    bus.subscribe("agent.started", _on_ev)

    async def on_start(cmd: StartAgentCmd) -> None:
        await cbus.publish_event(
            DomainEvent(type="agent.started", aggregate_id=cmd.aggregate_id)
        )

    cbus.register(StartAgentCmd, on_start)
    await cbus.send(StartAgentCmd("a1"))
    await asyncio.sleep(0.01)
    assert len(emitted) == 1
    assert emitted[0].aggregate_id == "a1"
    # event also landed in the store via publish_event
    assert store.count() == 1


@pytest.mark.asyncio
async def test_cqrs_read_model_projection_and_query() -> None:
    bus = EventBus()
    store = EventStore()
    rm = AgentStatusReadModel()
    bus.subscribe("agent.started", lambda e: asyncio.create_task(rm.handle(e)))
    bus.subscribe("agent.stopped", lambda e: asyncio.create_task(rm.handle(e)))

    await store.append(DomainEvent(type="agent.started", aggregate_id="a9", payload={}))
    await store.append(DomainEvent(type="agent.stopped", aggregate_id="a9", payload={}))
    # fold the store into the projection (simulates startup replay)
    for ev in await store.read_all():
        await rm.handle(ev)
    assert rm.state["a9"]["state"] == "stopped"

    qbus = QueryBus()
    qbus.register(GetAgentStatusQuery, lambda q: _ret(rm.state.get(q.agent_id, {})))
    result = await qbus.ask(GetAgentStatusQuery("a9"))
    assert result["state"] == "stopped"


@pytest.mark.asyncio
async def test_capability_metrics_read_model() -> None:
    rm = CapabilityMetricsReadModel()
    await rm.handle(DomainEvent(type="capability.executed", aggregate_id="desktop.click", payload={"duration_ms": 10.0}))
    await rm.handle(DomainEvent(type="capability.executed", aggregate_id="desktop.click", payload={"duration_ms": 30.0}))
    assert rm.metrics["desktop.click"]["count"] == 2
    assert rm.metrics["desktop.click"]["total_ms"] == 40.0




@pytest.mark.asyncio
async def test_domain_event_fields_defaults() -> None:
    ev = DomainEvent(type="x", aggregate_id="agg1")
    assert ev.aggregate_id == "agg1"
    assert isinstance(ev.timestamp, datetime)
    assert ev.version == 1
    assert ev.type == "x"


@pytest.mark.asyncio
async def test_event_store_sqlite_roundtrip(tmp_path) -> None:
    db = tmp_path / "events.db"
    store = EventStore(sqlite_path=str(db))
    await store.append(DomainEvent(type="desktop.clicked", aggregate_id="a1", payload={"x": 1}))
    all_ev = await store.read_all()
    assert len(all_ev) == 1
    assert all_ev[0].aggregate_id == "a1"
    store.close()


@pytest.mark.asyncio
async def test_command_bus_unknown_command_raises() -> None:
    cbus = CommandBus(EventBus())
    with pytest.raises(KeyError):
        await cbus.send(StartAgentCmd("a1"))
