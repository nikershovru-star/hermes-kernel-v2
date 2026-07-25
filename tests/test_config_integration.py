"""tests/test_config_integration.py — ADR-030 cross-component integration.

Verifies the vault wires optionally into AgentRuntime, WorkflowEngine, McpGateway
and PluginMarketplace, and that everything is a zero-regression no-op when
``vault=None``. asyncio_mode = auto.
"""

from __future__ import annotations

import pytest

from kernel.agent import AgentRuntime, BaseAgent
from kernel.bus import EventBus
from kernel.capability import CapabilityExecutor
from kernel.config_domain import ConfigScope
from kernel.config_vault import ConfigVault, _Base64Cipher
from kernel.domain import Agent, Artifact, Task, Workflow, WorkflowStep
from kernel.events import EventStore
from kernel.marketplace import PluginMarketplace
from kernel.marketplace_domain import PluginPackage, PluginSource, PluginStatus
from kernel.mcp_gateway import McpGateway
from kernel.workflow import WorkflowEngine


class _EchoAgent(BaseAgent):
    """Minimal runnable agent that echoes the task's interpolated parameters."""

    async def start(self) -> str:
        return self._entity.id

    async def stop(self, agent_id: str) -> bool:
        return True

    async def execute(self, agent_id: str, task: Task) -> Artifact:
        return Artifact(
            type="result",
            content={"params": getattr(task, "parameters", None), "metadata": task.metadata},
            format="json",
        )

    async def status(self, agent_id: str) -> dict:
        return {"state": "running"}

    async def get_capabilities(self) -> list:
        return []


class _MockHttp:
    def __init__(self):
        self.last = None

    async def post(self, url, json):
        self.last = json
        method = json["method"]
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": json["id"], "result": {"serverInfo": {"name": "s", "version": "1"}, "capabilities": {"tools": {}}}}
        return {"jsonrpc": "2.0", "id": json["id"], "result": {"ok": True}}


def _vault():
    return ConfigVault(cipher=_Base64Cipher())


async def _runtime_with_agent(vault=None, agent_id="ag1"):
    rt = AgentRuntime(vault=vault)
    agent = _EchoAgent(Agent(id=agent_id, name="echo", capabilities=[]))
    await rt.start(agent)
    return rt


# 1 — AgentRuntime.resolve_secret scoped to agent
async def test_agent_resolve_secret_scoped():
    v = _vault()
    await v.set_secret("apikey", "AA", scope=ConfigScope.AGENT, scope_id="ag1")
    await v.set_secret("apikey", "BB", scope=ConfigScope.AGENT, scope_id="ag2")
    rt = await _runtime_with_agent(vault=v)
    assert await rt.resolve_secret("ag1", "apikey") == "AA"
    # get_config proxy too
    await v.set("region", "eu", scope=ConfigScope.AGENT, scope_id="ag1")
    assert await rt.get_config("ag1", "region") == "eu"


# 2 — AgentRuntime.execute interpolates ${secrets.X}
async def test_agent_execute_interpolates_secret():
    v = _vault()
    await v.set_secret("apikey", "TOP-SECRET", scope=ConfigScope.AGENT, scope_id="ag1")
    rt = await _runtime_with_agent(vault=v)
    task = Task(name="t", parameters={"auth": "${secrets.apikey}", "n": 3})
    art = await rt.execute("ag1", task)
    assert art.content["params"] == {"auth": "TOP-SECRET", "n": 3}


# 3 — WorkflowEngine step resolves ${config.Y}
async def test_workflow_step_resolves_config():
    v = _vault()
    captured = {}

    async def handler(params, context):
        captured["params"] = params
        return Artifact(type="r", content=params, format="json")

    eng = WorkflowEngine(
        AgentRuntime(), CapabilityExecutor(handlers={"do": handler}),
        EventBus(), EventStore(), vault=v,
    )
    wf = Workflow(name="w", steps=[WorkflowStep(id="s1", name="s1", capability="do", input_mapping={"env": "${config.stage}"})])
    inst = await eng.start(wf)
    await v.set("stage", "prod", scope=ConfigScope.WORKFLOW, scope_id=inst.id)
    await eng.execute_step(inst, wf)
    assert captured["params"] == {"env": "prod"}


# 4 — MCP Gateway connect resolves auth_token from vault
async def test_mcp_connect_resolves_auth_from_vault():
    v = _vault()
    await v.set_secret("mcp:http://srv:auth_token", "TKN", scope=ConfigScope.MCP_SERVER, scope_id="http://srv")
    http = _MockHttp()
    gw = McpGateway(http_client=http, vault=v)
    await gw.connect("http://srv")  # no explicit token
    assert gw.auth_source("http://srv") == "vault"
    assert http.last["params"]["_meta"]["authorization"] == "Bearer TKN"


# 5 — PluginMarketplace.install fails on missing required_secrets
async def test_marketplace_install_fails_missing_secrets():
    v = _vault()
    mp = PluginMarketplace(vault=v)
    pkg = PluginPackage(package_id="p1", name="P", version="1.0.0", source=PluginSource("marketplace"), entrypoint="x", required_secrets=["db_pass"])
    result = await mp.install(pkg)
    assert result.status == PluginStatus.FAILED


# 6 — PluginMarketplace.install succeeds when secrets present
async def test_marketplace_install_succeeds_with_secrets():
    v = _vault()
    await v.set_secret("db_pass", "pw", scope=ConfigScope.PLUGIN, scope_id="p1")
    events = []

    class _Bus:
        def publish(self, e):
            events.append(e)

    mp = PluginMarketplace(vault=v, event_bus=_Bus())
    pkg = PluginPackage(package_id="p1", name="P", version="1.0.0", source=PluginSource("marketplace"), entrypoint="x", required_secrets=["db_pass"])
    result = await mp.install(pkg)
    assert result.status == PluginStatus.INSTALLED
    installed = [e for e in events if e.type == "mp.plugin_installed"]
    assert installed and installed[0].payload["secrets_resolved"] is True


# 7 — Zero regression: components work with vault=None
async def test_zero_regression_vault_none():
    # agent: params pass through untouched, resolve_secret raises honestly
    rt = await _runtime_with_agent(vault=None)
    task = Task(name="t", parameters={"a": "${secrets.x}"})
    art = await rt.execute("ag1", task)
    assert art.content["params"] == {"a": "${secrets.x}"}
    with pytest.raises(RuntimeError):
        await rt.resolve_secret("ag1", "x")
    assert await rt.get_config("ag1", "k", default="d") == "d"
    # workflow: no interpolation, no defaults seeding
    captured = {}

    async def handler(params, context):
        captured["params"] = params
        return Artifact(type="r", content=params, format="json")

    eng = WorkflowEngine(AgentRuntime(), CapabilityExecutor(handlers={"do": handler}), EventBus(), EventStore())
    wf = Workflow(name="w", steps=[WorkflowStep(id="s1", name="s1", capability="do", input_mapping={"x": "${config.y}"})])
    inst = await eng.start(wf)
    await eng.execute_step(inst, wf)
    assert captured["params"] == {"x": "${config.y}"}
    # marketplace: required_secrets precondition skipped without a vault
    mp = PluginMarketplace()
    pkg = PluginPackage(package_id="p2", name="Q", version="1.0.0", source=PluginSource("marketplace"), entrypoint="x", required_secrets=["z"])
    assert (await mp.install(pkg)).status == PluginStatus.INSTALLED


# 8 — ConfigChanged emitted on bus
async def test_config_changed_emitted_on_bus():
    events = []

    class _Bus:
        def publish(self, e):
            events.append(e)

    v = ConfigVault(cipher=_Base64Cipher(), event_bus=_Bus())
    await v.set("k", "v", scope=ConfigScope.AGENT, scope_id="a1")
    assert any(e.type == "cfg.config_changed" and e.aggregate_id == "agent:a1" for e in events)


# 9 — SecretAccessed audit entries queryable
async def test_secret_accessed_queryable():
    store = EventStore()
    v = ConfigVault(cipher=_Base64Cipher(), event_store=store)
    await v.set_secret("tok", "v", scope=ConfigScope.GLOBAL)
    await v.resolve_secret("tok", scope=ConfigScope.GLOBAL, accessor="svc")
    events = await store.read_stream("tok")
    accessed = [e for e in events if e.type == "cfg.secret_accessed"]
    assert accessed and accessed[0].payload["accessor"] == "svc"
    assert accessed[0].payload["action"] == "resolve"


# 10 — Vault cipher mock deterministic (injectable)
async def test_cipher_mock_deterministic():
    class _DetCipher:
        last_nonce = b"N"
        last_tag = b"T"
        calls = 0

        async def encrypt(self, plaintext: str) -> bytes:
            _DetCipher.calls += 1
            return b"X" + plaintext.encode()

        async def decrypt(self, ciphertext: bytes) -> str:
            return ciphertext[1:].decode()

    c = _DetCipher()
    v = ConfigVault(cipher=c)
    await v.set_secret("k", "hello", scope=ConfigScope.GLOBAL)
    raw = v._secrets["global:global:k"]
    assert raw.ciphertext == b"Xhello"
    assert raw.nonce == b"N" and raw.tag == b"T"
    assert await v.resolve_secret("k", scope=ConfigScope.GLOBAL) == "hello"
