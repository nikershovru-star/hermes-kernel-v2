"""tests/test_agent_runtime.py — unified BaseAgent lifecycle + AgentRuntime (ADR-016)."""

from __future__ import annotations

import pytest

from kernel.agent import AgentRuntime, BaseAgent
from kernel.domain import Agent, Artifact, Task
from plugins.builtin.agents.echo_agent import EchoAgent


@pytest.mark.asyncio
async def test_base_agent_lifecycle_via_runtime() -> None:
    runtime = AgentRuntime()
    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    agent = EchoAgent(entity)

    # start -> returns agent_id, registered as running
    aid = await runtime.start(agent)
    assert aid == entity.id
    assert aid in runtime.list()
    assert (await runtime.status(aid))["state"] == "running"

    # execute -> returns unified Artifact
    task = Task(name="say-hi", capability="hermes.agent.echo")
    artifact = await runtime.execute(aid, task)
    assert isinstance(artifact, Artifact)
    assert artifact.type == "hermes.agent.echo"
    assert artifact.content["echo"] == "say-hi"
    assert artifact.source == "agent:echo"
    assert f"task:{task.id}" in artifact.provenance

    # stop -> removed from registry
    assert await runtime.stop(aid) is True
    assert aid not in runtime.list()
    assert (await runtime.status(aid))["state"] == "offline"


@pytest.mark.asyncio
async def test_execute_unknown_agent_raises() -> None:
    runtime = AgentRuntime()
    task = Task(name="x", capability="hermes.agent.echo")
    with pytest.raises(KeyError):
        await runtime.execute("nope", task)


@pytest.mark.asyncio
async def test_execute_before_start_raises() -> None:
    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    agent = EchoAgent(entity)
    task = Task(name="x", capability="hermes.agent.echo")
    with pytest.raises(RuntimeError):
        await agent.execute(entity.id, task)


def test_echo_agent_is_base_agent() -> None:
    entity = Agent(name="echo", capabilities=["hermes.agent.echo"])
    assert isinstance(EchoAgent(entity), BaseAgent)
