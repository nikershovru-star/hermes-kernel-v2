"""tests/test_resilience_integration.py — ADR-031 cross-component integration.

Verifies the ResilienceEngine wires optionally into McpGateway, WorkflowEngine,
AgentRuntime and that everything is a zero-regression no-op when
``resilience=None``. Also covers metrics counter increments and DLQ replay.
asyncio_mode = auto.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kernel.agent import AgentRuntime, BaseAgent
from kernel.bus import EventBus
from kernel.capability import CapabilityExecutor
from kernel.domain import Agent, Artifact, RetryPolicy, Task, Workflow, WorkflowStep
from kernel.events import EventStore
from kernel.mcp_gateway import McpGateway
from kernel.resilience import ResilienceEngine
from kernel.resilience_domain import (
    CircuitBreakerOpenError,
    CircuitState,
    ResilienceCircuitConfig,
    ResilienceDeadLetterEntry,
    RetryExhaustedError,
)
from kernel.resilience_store import ResilienceStore
from kernel.workflow import WorkflowEngine


class _Clock:
    def __init__(self, start=None):
        self._t = start or datetime(2026, 7, 25, tzinfo=timezone.utc)

    def __call__(self):
        return self._t

    def advance_ms(self, ms):
        self._t = self._t + timedelta(milliseconds=ms)


async def _noop_sleep(_s):
    return None


class _Metrics:
    def __init__(self):
        self.records = []

    async def record_metric(self, name, value, labels=None):
        self.records.append((name, value, labels or {}))


class _FlakyAgent(BaseAgent):
    def __init__(self, entity: Agent, fail_times: int = 0):
        super().__init__(entity)
        self.n = 0
        self.fail_times = fail_times

    async def start(self):
        return self._entity.id

    async def stop(self, agent_id):
        return True

    async def execute(self, agent_id, task):
        self.n += 1
        if self.n <= self.fail_times:
            raise ConnectionError("transient")
        return Artifact(type="result", content={"ok": True}, format="json")

    async def status(self, agent_id):
        return {"state": "running"}

    async def get_capabilities(self):
        return []


class _FailHandler:
    async def execute(self, params, context):
        raise ConnectionError("boom")


class _OkHandler:
    async def execute(self, params, context):
        return Artifact(type="ok", content={"v": params.get("x")}, format="json")


# 1 — MCP call_tool guarded by circuit (opens after threshold)
async def test_mcp_gateway_circuit():
    clk = _Clock()
    eng = ResilienceEngine(clock=clk, sleep=_noop_sleep)
    eng.register_circuit("mcp:http://srv", ResilienceCircuitConfig(name="mcp:http://srv", failure_threshold=3, recovery_timeout_ms=1000))

    class _FailHttp:
        async def post(self, url, json):
            raise ConnectionError("server down")

    gw = McpGateway(http_client=_FailHttp(), resilience=eng, max_retries=0)
    for _ in range(3):
        art = await gw.call_tool("http://srv", "t", {})
        assert art.type == "error"
    assert eng.get_circuit_status("mcp:http://srv") is CircuitState.OPEN
    # OPEN → next call rejected fast (still error artifact, no http hit)
    art = await gw.call_tool("http://srv", "t", {})
    assert art.type == "error"


# 2 — MCP gateway works unchanged without resilience
async def test_mcp_gateway_zero_regression():
    class _OkHttp:
        async def post(self, url, json):
            m = json["method"]
            if m == "initialize":
                return {"jsonrpc": "2.0", "id": json["id"], "result": {"serverInfo": {"name": "s", "version": "1"}, "capabilities": {"tools": {}}}}
            return {"jsonrpc": "2.0", "id": json["id"], "result": {"content": [{"type": "text", "text": "ok"}]}}

    gw = McpGateway(http_client=_OkHttp())
    await gw.connect("http://srv")
    art = await gw.call_tool("http://srv", "t", {})
    assert art.type == "mcp_tool_result"


# 3 — WorkflowEngine step with circuit + DLQ on exhaustion
async def test_workflow_step_dlq_on_exhaustion():
    eng = ResilienceEngine(sleep=_noop_sleep)
    ce = CapabilityExecutor(handlers={"do": _FailHandler()})
    we = WorkflowEngine(AgentRuntime(), ce, EventBus(), EventStore(), resilience=eng)
    wf = Workflow(
        name="w",
        steps=[WorkflowStep(id="s1", name="s1", capability="do", retry_policy=RetryPolicy(max_attempts=1))],
    )
    inst = await we.start(wf)
    art = await we.execute_step(inst, wf)
    assert art.type == "error"
    pending = eng.list_dead_letter("pending")
    assert len(pending) == 1
    assert pending[0].original_task["step_id"] == "s1"


# 4 — WorkflowEngine zero regression without resilience
async def test_workflow_zero_regression():
    ce = CapabilityExecutor(handlers={"do": _FailHandler()})
    we = WorkflowEngine(AgentRuntime(), ce, EventBus(), EventStore())
    wf = Workflow(
        name="w",
        steps=[WorkflowStep(id="s1", name="s1", capability="do", retry_policy=RetryPolicy(max_attempts=1))],
    )
    inst = await we.start(wf)
    art = await we.execute_step(inst, wf)
    assert art.type == "error"
    assert we._resilience is None  # no DLQ enqueued


# 5 — AgentRuntime.execute with retry recovers on 2nd attempt
async def test_agent_execute_retry():
    eng = ResilienceEngine(sleep=_noop_sleep)
    rt = AgentRuntime(resilience=eng)
    agent = _FlakyAgent(Agent(id="ag1", name="f", capabilities=[]), fail_times=1)
    await rt.start(agent)
    art = await rt.execute("ag1", Task(name="t"))
    assert art.content == {"ok": True}
    assert agent.n == 2


# 6 — AgentRuntime.get_circuit_status proxy + RuntimeError when unwired
async def test_agent_circuit_status_proxy():
    eng = ResilienceEngine(sleep=_noop_sleep)
    eng.register_circuit("svc", ResilienceCircuitConfig(name="svc"))
    rt = AgentRuntime(resilience=eng)
    assert rt.get_circuit_status("svc") is CircuitState.CLOSED
    rt2 = AgentRuntime()
    with pytest.raises(RuntimeError):
        rt2.get_circuit_status("svc")


# 7 — metrics: circuit_opened increments counter (integration via engine+metrics)
async def test_metrics_circuit_opened_integration():
    m = _Metrics()
    eng = ResilienceEngine(sleep=_noop_sleep, metrics=m)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=2, recovery_timeout_ms=500))
    clk = _Clock()
    eng._clock = clk
    for _ in range(2):
        with pytest.raises(ConnectionError):
            async with eng.call_with_circuit("c"):
                raise ConnectionError("boom")
    opened = [r for r in m.records if r[0] == "res.circuit_opened"]
    assert len(opened) == 1


# 8 — DLQ replay success
async def test_dlq_replay_success_integration():
    eng = ResilienceEngine()
    e = await eng.enqueue_dead_letter({"task": "foo"}, "fail", 2)

    async def replay(task):
        return "done:" + task["task"]

    res = await eng.replay_dead_letter(e.entry_id, replay)
    assert res == "done:foo"
    assert eng.list_dead_letter("pending") == []
    assert len(eng.list_dead_letter("replayed")) == 1


# 9 — DLQ replay failure keeps pending (honest: not auto-retried)
async def test_dlq_replay_failure_keeps_pending():
    eng = ResilienceEngine()
    e = await eng.enqueue_dead_letter({"task": "bar"}, "fail", 2)
    with pytest.raises(ConnectionError):
        await eng.replay_dead_letter(e.entry_id, lambda task: (_ for _ in ()).throw(ConnectionError("still")))
    pending = eng.list_dead_letter("pending")
    assert len(pending) == 1 and pending[0].entry_id == e.entry_id


# 10 — full wiring: store + engine persist circuit & DLQ, repo-reload survives
async def test_store_engine_persist_and_reload():
    import tempfile, os
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    st = ResilienceStore(db_path=path)
    eng = ResilienceEngine(store=st, sleep=_noop_sleep)
    eng.register_circuit("c", ResilienceCircuitConfig(name="c", failure_threshold=1, recovery_timeout_ms=500))
    with pytest.raises(ConnectionError):
        async with eng.call_with_circuit("c"):
            raise ConnectionError("boom")
    await eng.enqueue_dead_letter({"x": 1}, "err", 1)
    # reload the store and confirm persistence
    st.reload()
    assert st.get_circuit("c")["state"] == "open"
    assert len(st.list_dead_letter("pending")) == 1
    st.reload(None)
    try:
        os.remove(path)
    except (OSError, PermissionError):
        pass


# 11 — retry exhausted propagates RetryExhaustedError through AgentRuntime
async def test_agent_retry_exhausted_error():
    eng = ResilienceEngine(sleep=_noop_sleep)
    rt = AgentRuntime(resilience=eng)
    agent = _FlakyAgent(Agent(id="ag2", name="f2", capabilities=[]), fail_times=99)
    await rt.start(agent)
    with pytest.raises(RetryExhaustedError):
        await rt.execute("ag2", Task(name="t", retry=None))
