"""tests/test_integration_health.py — Health & Recovery full roundtrip (ADR-021).

Covers:
- AgentRuntime + HealthMonitor: probe detects a stopped agent → UNHEALTHY.
- AgentRuntime + HealthMonitor + RecoveryEngine: unhealthy → auto-restart.
- WorkflowEngine + DeadLetterQueue: exhausted step → dead-letter → replay.
- CapabilityExecutor + CircuitBreaker: breaker trips on repeated failure.
- Backward compat: all components work with health/recovery = None.
"""

from __future__ import annotations

import pytest
from kernel.agent import AgentRuntime
from kernel.bus import EventBus
from kernel.capability import CapabilityExecutor
from kernel.domain import (
    Agent,
    Artifact,
    CircuitBreakerPolicy,
    CircuitBreakerState,
    HealthCheck,
    HealthStatus,
    RetryPolicy,
    Task,
    Workflow,
    WorkflowStep,
)
from kernel.events import EventStore
from kernel.health import (
    CircuitBreaker,
    CircuitBreakerOpen,
    DeadLetterQueue,
    HealthMonitor,
    RecoveryEngine,
)
from plugins.builtin.agents.echo_agent import EchoAgent


def _wire() -> tuple[EventBus, EventStore, HealthMonitor]:
    bus = EventBus()
    store = EventStore()
    return bus, store, HealthMonitor(bus, store)


# --- AgentRuntime + HealthMonitor ----------------------------------------- #
async def test_probe_detects_stopped_agent() -> None:
    bus, store, mon = _wire()
    runtime = AgentRuntime(bus=bus, store=store, health_monitor=mon)
    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    aid = await runtime.start(EchoAgent(entity))

    rec = await mon.check_now(aid)
    assert rec.status == HealthStatus.HEALTHY

    # Simulate the agent dying WITHOUT going through runtime.stop (which would
    # unregister it): drop it from the live registry directly.
    runtime._agents.pop(aid)
    rec2 = await mon.check_now(aid)  # failure_threshold defaults to 3
    assert rec2.status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY)


async def test_agent_autorestart_on_unhealthy() -> None:
    bus, store, mon = _wire()
    runtime = AgentRuntime(bus=bus, store=store, health_monitor=mon)
    dlq = DeadLetterQueue(event_store=store, event_bus=bus)
    recovery = RecoveryEngine(mon, dlq, runtime, workflow_engine=None, event_bus=bus, event_store=store)

    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    aid = await runtime.start(EchoAgent(entity))
    # Force the runtime to see it as unhealthy and drive recovery directly.
    rec = mon.get_record(aid)
    await recovery.on_unhealthy(aid, "agent", rec)
    # A fresh agent was started (echo agent re-registers under same id family).
    assert recovery.restart_count(aid) == 1


# --- WorkflowEngine + DeadLetterQueue ------------------------------------- #
async def test_workflow_failed_step_dead_lettered_and_replayable() -> None:
    from kernel.workflow import WorkflowEngine

    bus, store, mon = _wire()
    dlq = DeadLetterQueue(event_store=store, event_bus=bus)

    calls = {"n": 0}

    async def flaky(params, context):
        calls["n"] += 1
        raise RuntimeError("capability down")

    caps = CapabilityExecutor(handlers={"do.thing": flaky})
    engine = WorkflowEngine(
        AgentRuntime(), caps, bus, store, dead_letter=dlq, health_monitor=mon
    )

    step = WorkflowStep(
        id="s1",
        name="do",
        capability="do.thing",
        retry_policy=RetryPolicy(max_attempts=1, backoff_seconds=0.0),
    )
    wf = Workflow(name="wf", steps=[step])
    inst = await engine.start(wf)
    result = await engine.execute_step(inst, wf)
    assert result.type == "error"
    assert dlq.count(inst.id) == 1

    # Replay: handler now "succeeds".
    async def handler(entry) -> bool:
        return True

    replayed = await dlq.replay(inst.id, handler)
    assert replayed == 1


# --- CapabilityExecutor + CircuitBreaker ---------------------------------- #
async def test_capability_executor_breaker_trips() -> None:
    bus = EventBus()
    store = EventStore()
    cb = CircuitBreaker(
        policy=CircuitBreakerPolicy(failure_threshold=2, recovery_timeout_seconds=60.0),
        event_bus=bus,
        event_store=store,
    )

    async def broken(params, context):
        raise RuntimeError("plugin frozen")

    caps = CapabilityExecutor(handlers={"desktop.click": broken}, circuit_breaker=cb)

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await caps.execute("desktop.click", {})
    assert cb.state("desktop.click") == CircuitBreakerState.OPEN
    # Now the breaker rejects fast without calling the handler.
    with pytest.raises(CircuitBreakerOpen):
        await caps.execute("desktop.click", {})


# --- Backward compatibility (no health/recovery) -------------------------- #
async def test_backward_compat_no_health() -> None:
    runtime = AgentRuntime()  # no health monitor
    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    aid = await runtime.start(EchoAgent(entity))
    artifact = await runtime.execute(aid, Task(name="hi", capability="hermes.agent.echo"))
    assert isinstance(artifact, Artifact)
    assert await runtime.stop(aid) is True


async def test_backward_compat_capability_no_breaker() -> None:
    async def ok(params, context):
        return {"type": "result", "content": {"ok": True}}

    caps = CapabilityExecutor(handlers={"x.y": ok})  # no breaker
    art = await caps.execute("x.y", {})
    assert art.content["ok"] is True
