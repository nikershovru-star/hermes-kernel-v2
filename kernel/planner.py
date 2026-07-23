"""kernel/planner.py — rule-based Workflow planner (ADR-019).

Given a high-level goal, produces a ``Workflow`` definition by matching intent
keywords against available capabilities. This is the v2.5.0 STATIC planner;
dynamic LLM-based replanning is deferred to ADR-023.

AXIS CONTRACT: depends on kernel.domain + kernel.capability (CapabilityRegistry).
Never imports plugins. Capabilities are passed in (registry lookup only).
"""

from __future__ import annotations

import logging
from typing import Any

from kernel.capability import CapabilityRegistry
from kernel.domain import Workflow, WorkflowStep, WorkflowStatus, WorkflowTrigger

logger = logging.getLogger("hermes.kernel.planner")

# Intent keyword -> ordered list of (step_id, capability, name) templates.
# A simple, extensible rule table (not ML).
_GOAL_TEMPLATES: dict[str, list[tuple[str, str, str]]] = {
    "login": [
        ("find_browser", "desktop.screenshot", "Screenshot desktop"),
        ("read_label", "desktop.ocr", "OCR read labels"),
        ("click_signin", "desktop.click", "Click Sign in"),
        ("enter_user", "desktop.type", "Type username"),
        ("click_next", "desktop.click", "Click Next"),
    ],
    "screenshot": [
        ("shot", "desktop.screenshot", "Capture screenshot"),
    ],
    "click": [
        ("shot", "desktop.screenshot", "Screenshot"),
        ("ocr", "desktop.ocr", "OCR locate target"),
        ("click", "desktop.click", "Click target"),
    ],
    "fill_form": [
        ("shot", "desktop.screenshot", "Screenshot form"),
        ("ocr", "desktop.ocr", "OCR read fields"),
        ("type", "desktop.type", "Type into field"),
        ("submit", "desktop.click", "Click submit"),
    ],
}


class Planner:
    """Static, rule-based planner (ADR-019)."""

    def __init__(self, capability_registry: CapabilityRegistry | None = None) -> None:
        self._registry = capability_registry

    async def plan(self, goal: str, available_caps: list[str] | None = None) -> Workflow:
        """Generate a Workflow from a natural-language goal.

        ``available_caps`` (if given) prunes steps whose capability is not
        available; the registry (if set) is consulted when caps are omitted.
        """
        caps = available_caps
        if caps is None and self._registry is not None:
            caps = [c.name for c in await self._registry.list()]

        template_key = self._match_template(goal)
        steps_raw = _GOAL_TEMPLATES.get(template_key, _GOAL_TEMPLATES["click"])

        steps: list[WorkflowStep] = []
        for sid, cap, name in steps_raw:
            if caps is not None and cap not in caps:
                continue  # skip unavailable capabilities (honest degradation)
            steps.append(
                WorkflowStep(id=sid, name=name, capability=cap)
            )
        if not steps:
            # fallback: a single no-op screenshot step so the workflow is runnable
            steps.append(WorkflowStep(id="shot", name="Screenshot", capability="desktop.screenshot"))

        return Workflow(
            name=goal,
            description=f"Planned workflow for goal: {goal}",
            steps=steps,
            status=WorkflowStatus.DRAFT,
            trigger=WorkflowTrigger(type="manual"),
        )

    @staticmethod
    def _match_template(goal: str) -> str:
        g = goal.lower()
        if "log in" in g or "login" in g or "sign in" in g:
            return "login"
        if "fill" in g and "form" in g:
            return "fill_form"
        if "screenshot" in g or "capture" in g:
            return "screenshot"
        if "click" in g or "press" in g or "tap" in g:
            return "click"
        return "click"  # default: locate + click


__all__ = ["Planner"]
