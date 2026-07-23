"""tests/test_planner.py — rule-based Planner (ADR-019).

Verifies goal → Workflow generation, capability pruning, and the default
fallback. The planner is rule-based (templates), not LLM (documented in ADR-019).
"""

from __future__ import annotations

import asyncio

import pytest
from kernel.capability import CapabilityRegistry
from kernel.domain import Capability, Workflow, WorkflowStep, WorkflowStatus
from kernel.planner import Planner


@pytest.mark.asyncio
async def test_plan_login_goal_generates_steps() -> None:
    p = Planner()
    wf = await p.plan("log in to gmail")
    assert isinstance(wf, Workflow)
    assert wf.status == WorkflowStatus.DRAFT
    caps = [s.capability for s in wf.steps]
    assert "desktop.screenshot" in caps
    assert "desktop.click" in caps
    assert "desktop.type" in caps


@pytest.mark.asyncio
async def test_plan_prunes_unavailable_capabilities() -> None:
    p = Planner()
    # only desktop.click is available
    wf = await p.plan("log in to gmail", available_caps=["desktop.click"])
    caps = [s.capability for s in wf.steps]
    assert caps == ["desktop.click", "desktop.click"]


@pytest.mark.asyncio
async def test_plan_fallback_when_all_unavailable() -> None:
    p = Planner()
    wf = await p.plan("do something weird", available_caps=[])
    # fallback keeps the workflow runnable (single screenshot step)
    assert len(wf.steps) == 1
    assert wf.steps[0].capability == "desktop.screenshot"


@pytest.mark.asyncio
async def test_plan_uses_registry_when_caps_omitted() -> None:
    reg = CapabilityRegistry.__new__(CapabilityRegistry)
    reg._caps = {  # type: ignore[attr-defined]
        "c1": Capability(name="desktop.click"),
        "c2": Capability(name="desktop.screenshot"),
    }
    reg._lock = asyncio.Lock()  # type: ignore[attr-defined]
    p = Planner(reg)
    wf = await p.plan("click the button")
    caps = {s.capability for s in wf.steps}
    assert caps <= {"desktop.click", "desktop.screenshot", "desktop.ocr"}


@pytest.mark.asyncio
async def test_plan_click_goal_has_ocr_and_click() -> None:
    p = Planner()
    wf = await p.plan("click the Submit button", available_caps=[
        "desktop.screenshot", "desktop.ocr", "desktop.click"
    ])
    caps = [s.capability for s in wf.steps]
    assert caps == ["desktop.screenshot", "desktop.ocr", "desktop.click"]


@pytest.mark.asyncio
async def test_plan_empty_goal_uses_fallback() -> None:
    p = Planner()
    wf = await p.plan("")
    assert isinstance(wf, Workflow)
    # fallback always yields at least the screenshot capability
    assert any(s.capability == "desktop.screenshot" for s in wf.steps)


@pytest.mark.asyncio
async def test_plan_unknown_goal_keeps_runnable() -> None:
    p = Planner()
    # unknown goal with only ocr available -> fallback pruned to ocr only
    wf = await p.plan("do something totally unknown", available_caps=["desktop.ocr"])
    caps = [s.capability for s in wf.steps]
    assert all(c == "desktop.ocr" for c in caps)
