"""tests/test_recovery_engine.py — RecoveryEngine decision tree (ADR-021).

Covers: agent restart on unhealthy (mocked runtime), workflow → dead-letter,
max-restart escalation → dead-letter + no further restart, on_dead_letter
decisions (retry / escalate / archive), and event-driven subscription path.
"""

from __future__ import annotations

import uuid

import pytest
from kernel.bus import EventBus
from kernel.domain import DeadLetterEntry, HealthCheck, HealthRecord, HealthStatus
from kernel.events import AgentUnhealthy, EventStore
from kernel.health import DeadLetterQueue, HealthMonitor, RecoveryEngine


class _FakeAgentRuntime:
    """Duck-typed stand-in for AgentRuntime (get/stop/start)."""

    def __init__(self, *, present: bool = True, fail_start: bool = False) -> None:
        self._present = present
        self._fail_start = fail_start
        self.stopped: list[str] = []
        self.started: list[str] = []

    def get(self, agent_id: str):
        return object() if self._present else None

    async def stop(self, agent_id: str) -> bool:
        self.stopped.append(agent_id)
        return True

    async def start(self, agent) -> str:
        if self._fail_start:
            raise RuntimeError("start failed")
        self.started.append("x")
        return "x"


class _FakeWorkflowEngine:
    def __init__(self, *, present: bool = True) -> None:
        self._present = present

    def get_instance(self, instance_id: str):
        if not self._present:
            raise KeyError(instance_id)
        return object()


def _engine(agents=None, workflows=None, max_restarts: int = 3):
    bus = EventBus()
    store = EventStore()
    mon = HealthMonitor(bus, store)
    dlq = DeadLetterQueue(event_store=store, event_bus=bus)
    eng = RecoveryEngine(
        health_monitor=mon,
        dead_letter=dlq,
        agent_runtime=agents or _FakeAgentRuntime(),
        workflow_engine=workflows or _FakeWorkflowEngine(),
        event_bus=bus,
        event_store=store,
        max_restarts=max_restarts,
    )
    return eng, mon, dlq, bus, store


def _rec(cid: str, ctype: str = "agent") -> HealthRecord:
    return HealthRecord(component_id=cid, component_type=ctype, status=HealthStatus.UNHEALTHY, last_error="died")


async def test_agent_restart_on_unhealthy() -> None:
    agents = _FakeAgentRuntime()
    eng, _, _, _, _ = _engine(agents=agents)
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    assert agents.stopped == ["a1"]
    assert agents.started == ["x"]
    assert eng.restart_count("a1") == 1


async def test_agent_restart_failure_escalates_to_dead_letter() -> None:
    agents = _FakeAgentRuntime(fail_start=True)
    eng, _, dlq, _, _ = _engine(agents=agents)
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    assert dlq.count("a1") == 1
    assert eng.restart_count("a1") == 0


async def test_missing_agent_escalates() -> None:
    agents = _FakeAgentRuntime(present=False)
    eng, _, dlq, _, _ = _engine(agents=agents)
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    assert dlq.count("a1") == 1


async def test_workflow_unhealthy_dead_letters() -> None:
    eng, _, dlq, _, _ = _engine()
    await eng.on_unhealthy("w1", "workflow", _rec("w1", "workflow"))
    assert dlq.count("w1") == 1
    assert eng.restart_count("w1") == 1


async def test_max_restarts_exceeded_escalates() -> None:
    agents = _FakeAgentRuntime()
    eng, _, dlq, _, _ = _engine(agents=agents, max_restarts=2)
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    assert eng.restart_count("a1") == 2
    # third → escalate (no more restarts)
    await eng.on_unhealthy("a1", "agent", _rec("a1"))
    assert eng.restart_count("a1") == 2
    assert dlq.count("a1") == 1


async def test_on_dead_letter_decisions() -> None:
    eng, _, _, _, _ = _engine()
    fresh = DeadLetterEntry(entry_id="1", component_id="a1", entry_type="task", payload={}, error="x", retry_count=0, max_retries=3)
    assert await eng.on_dead_letter(fresh) == "retry"
    exhausted = DeadLetterEntry(entry_id="2", component_id="a1", entry_type="task", payload={}, error="x", retry_count=3, max_retries=3)
    assert await eng.on_dead_letter(exhausted) == "escalate"
    done = fresh.model_copy(update={"recovered_at": __import__("datetime").datetime.now()})
    assert await eng.on_dead_letter(done) == "archive"


async def test_start_subscribes_and_reacts_to_event() -> None:
    agents = _FakeAgentRuntime()
    eng, mon, _, bus, _ = _engine(agents=agents)
    # register a record so the engine can resolve component_type
    mon.register("a1", "agent", probe=_probe_false, check=HealthCheck(failure_threshold=1))
    await mon.check_now("a1")  # marks unhealthy + publishes AgentUnhealthy
    await eng.start()
    # publish another unhealthy event directly and let the bus dispatch
    import asyncio

    bus.publish(AgentUnhealthy(component_id="a1", last_error="died", consecutive_failures=1))
    await asyncio.sleep(0.02)
    assert "a1" in agents.stopped
    await eng.stop()


async def _probe_false() -> bool:
    return False
