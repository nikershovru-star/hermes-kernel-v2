"""kernel/workflow.py — Workflow Runtime engine (ADR-019, Execution Platform v5).

``WorkflowEngine`` is a state machine + execution engine for ``WorkflowInstance``.
It is NOT a replacement for ``AgentRuntime`` — it USES ``AgentRuntime`` (to run
steps that target a BaseAgent) and ``CapabilityExecutor`` (to run a capability
directly), and journals every transition through the existing ``EventBus`` /
``EventStore`` (ADR-017 reuse).

AXIS CONTRACT: depends on kernel.domain, kernel.events, kernel.agent,
kernel.capability. Never imports plugins. Agents/capabilities are injected.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

from kernel.agent import Agent, AgentRuntime
from kernel.capability import CapabilityExecutor
from kernel.domain import (
    Artifact,
    DeadLetterEntry,
    SandboxPolicy,
    Task,
    Workflow,
    WorkflowInstance,
    WorkflowStep,
    WorkflowStatus,
)
from kernel.health import DeadLetterQueue, HealthMonitor
from kernel.sandbox import Sandbox, SandboxError
from kernel.events import (
    DomainEvent,
    EventBus,
    EventStore,
    WorkflowCompensating,
    WorkflowStalled,
    WorkflowStepAwaitingApproval,
    WorkflowStepCompleted,
    WorkflowStepFailed,
    WorkflowStepStarted,
)

logger = logging.getLogger("hermes.kernel.workflow")


class WorkflowEngine:
    """State machine + execution engine for WorkflowInstance (ADR-019)."""

    def __init__(
        self,
        agent_runtime: AgentRuntime,
        capability_executor: CapabilityExecutor,
        event_bus: EventBus,
        event_store: EventStore,
        sandbox: Sandbox | None = None,
        health_monitor: HealthMonitor | None = None,
        dead_letter: DeadLetterQueue | None = None,
    ) -> None:
        self._agents = agent_runtime
        self._caps = capability_executor
        self._bus = event_bus
        self._store = event_store
        self._sandbox = sandbox
        self._health = health_monitor
        self._dlq = dead_letter
        self._instances: dict[str, WorkflowInstance] = {}

    # -- instance lifecycle ---------------------------------------------- #
    async def start(self, workflow: Workflow, context: dict[str, Any] | None = None) -> WorkflowInstance:
        """Create + start a WorkflowInstance from a Workflow definition."""
        inst = WorkflowInstance(
            workflow_id=workflow.id,
            status=WorkflowStatus.RUNNING,
            started_at=datetime.now(timezone.utc),
        )
        if workflow.steps:
            inst.current_step_id = workflow.steps[0].id
        if context:
            workflow.context.update(context)
        self._instances[inst.id] = inst
        logger.info("WorkflowEngine: started instance %s for workflow %s", inst.id, workflow.id)
        return inst

    def get_instance(self, instance_id: str) -> WorkflowInstance:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise KeyError(f"workflow instance {instance_id!r} not found")
        return inst

    async def get_status(self, instance_id: str) -> WorkflowInstance:
        return self.get_instance(instance_id)

    # -- step execution --------------------------------------------------- #
    async def execute_step(
        self,
        instance: WorkflowInstance,
        workflow: Workflow,
        agent: Agent | None = None,
        policy: SandboxPolicy | None = None,
    ) -> Artifact:
        """Execute the current step: resolve → map inputs → run → emit events.

        Handles retry (exponential backoff), compensation on exhaustion, and
        human-approval pausing. Returns the produced Artifact.
        """
        step = self._current_step(workflow, instance)
        if step is None:
            instance.status = WorkflowStatus.COMPLETED
            instance.completed_at = datetime.now(timezone.utc)
            return Artifact(type="workflow", content={"status": "completed"}, format="json")

        instance.current_step_id = step.id
        attempt = instance.step_attempts.get(step.id, 0) + 1
        instance.step_attempts[step.id] = attempt

        # human approval gate
        if step.requires_approval and attempt == 1:
            instance.status = WorkflowStatus.PAUSED
            await self._emit(WorkflowStepAwaitingApproval(instance.id, step.id, "requires_approval"))
            return Artifact(type="approval_required", content={"step_id": step.id}, format="json")

        await self._emit(WorkflowStepStarted(instance.id, step.id, step.capability))
        params = self._resolve_inputs(step, instance, workflow)

        try:
            started = time.monotonic()
            coro = self._run_step(step, params, agent, instance)
            if self._sandbox is None:
                artifact = await coro
            else:
                policy = policy or self._resolve_policy(step, workflow)
                artifact = await self._sandbox.run(
                    coro,
                    policy=policy,
                    cleanup=lambda: self._compensate_on_breach(instance, workflow),
                    context={"workflow_id": instance.id, "step_id": step.id},
                )
            duration_ms = (time.monotonic() - started) * 1000.0
            instance.step_results[step.id] = artifact.id
            instance.event_log.append(artifact.id)
            await self._emit(
                WorkflowStepCompleted(instance.id, step.id, artifact.id, duration_ms)
            )
            # advance to next step (linear scan over DAG steps)
            self._advance(instance, workflow)
            if instance.current_step_id is None:
                instance.status = WorkflowStatus.COMPLETED
                instance.completed_at = datetime.now(timezone.utc)
            return artifact
        except SandboxError:
            # sandbox breach is fatal for the step — propagate (cleanup already ran)
            raise
        except Exception as exc:  # noqa: BLE001 - engine must handle + emit
            will_retry = attempt < step.retry_policy.max_attempts
            await self._emit(
                WorkflowStepFailed(instance.id, step.id, str(exc), attempt, will_retry)
            )
            if will_retry:
                backoff = step.retry_policy.backoff_seconds
                if step.retry_policy.exponential:
                    backoff *= 2 ** (attempt - 1)
                await asyncio.sleep(backoff)
                return await self.execute_step(instance, workflow, agent)
            # exhausted -> dead-letter (optional) then compensate or fail
            if self._dlq is not None:
                await self._dlq.append(
                    DeadLetterEntry(
                        entry_id=f"{instance.id}:{step.id}:{attempt}",
                        component_id=instance.id,
                        entry_type="workflow_step",
                        payload={
                            "step_id": step.id,
                            "capability": step.capability,
                            "params": params,
                            "attempt": attempt,
                        },
                        error=str(exc),
                    )
                )
            await self._emit(WorkflowStalled(instance.id, step.id, str(exc)))
            if step.compensation:
                await self.compensate(instance, workflow, step.id)
            else:
                instance.status = WorkflowStatus.FAILED
            return Artifact(type="error", content={"error": str(exc)}, format="json")

    # -- human approval --------------------------------------------------- #
    async def approve(self, instance_id: str, step_id: str, approved: bool, workflow: Workflow) -> None:
        """Resume (approved) or fail/compensate (rejected) a paused workflow."""
        inst = self.get_instance(instance_id)
        if inst.status != WorkflowStatus.PAUSED:
            raise RuntimeError(f"instance {instance_id} is not paused")
        if approved:
            inst.status = WorkflowStatus.RUNNING
            # resume: drive remaining steps to completion (current step re-runs
            # without re-pausing because attempt > 1)
            while inst.status == WorkflowStatus.RUNNING and inst.current_step_id is not None:
                await self.execute_step(inst, workflow)
        else:
            step = self._step_by_id(workflow, step_id)
            if step and step.compensation:
                await self.compensate(inst, workflow, step_id)
            else:
                inst.status = WorkflowStatus.FAILED

    # -- compensation ----------------------------------------------------- #
    async def compensate(self, instance: WorkflowInstance, workflow: Workflow, failed_step: str) -> None:
        """Run compensation steps in reverse order from ``failed_step``.

        For every step strictly before ``failed_step`` that declares a
        ``compensation`` step, and for the ``failed_step`` itself if it declares
        one, run that compensation step. This is reverse-order compensation
        (not a full Saga rollback — see ADR-019 honest notes).
        """
        inst = instance
        inst.status = WorkflowStatus.COMPENSATING
        idx = self._step_index(workflow, failed_step)
        # steps before the failed one
        for i in range(idx - 1, -1, -1):
            step = workflow.steps[i]
            if step.compensation:
                await self._run_compensation(inst, workflow, failed_step, step.compensation)
        # compensation declared directly on the failed step
        failed = self._step_by_id(workflow, failed_step)
        if failed is not None and failed.compensation:
            await self._run_compensation(inst, workflow, failed_step, failed.compensation)
        inst.status = WorkflowStatus.FAILED

    async def _run_compensation(
        self, inst: WorkflowInstance, workflow: Workflow, failed_step: str, comp_step_id: str
    ) -> None:
        comp = self._step_by_id(workflow, comp_step_id)
        if comp is None:
            return
        await self._emit(WorkflowCompensating(inst.id, failed_step, comp_step_id))
        try:
            params = self._resolve_inputs(comp, inst, workflow)
            await self._run_step(comp, params, None)
            inst.step_results[comp_step_id] = comp_step_id
        except Exception as cexc:  # noqa: BLE001 - compensate best-effort
            logger.error("compensation %s failed: %s", comp_step_id, cexc)

    # -- sandbox helpers -------------------------------------------------- #
    def _resolve_policy(self, step: WorkflowStep, workflow: Workflow) -> SandboxPolicy:
        """Policy precedence: explicit step policy → workflow context policy → default."""
        if step.timeout_seconds and step.timeout_seconds < 30.0:
            return SandboxPolicy(timeout_seconds=step.timeout_seconds)
        wf_policy = workflow.context.get("sandbox_policy")
        if isinstance(wf_policy, dict):
            return SandboxPolicy(**{k: v for k, v in wf_policy.items() if k in SandboxPolicy.model_fields})
        return SandboxPolicy()

    async def _compensate_on_breach(self, instance: WorkflowInstance, workflow: Workflow) -> None:
        """Best-effort compensation when a sandbox breach cancels a step."""
        step = self._current_step(workflow, instance)
        if step is not None and step.compensation:
            try:
                await self.compensate(instance, workflow, step.id)
            except Exception as cexc:  # noqa: BLE001
                logger.error("sandbox breach compensation failed: %s", cexc)

    # -- internals -------------------------------------------------------- #
    def _current_step(self, workflow: Workflow, instance: WorkflowInstance) -> WorkflowStep | None:
        if instance.current_step_id is None:
            return None
        return self._step_by_id(workflow, instance.current_step_id)

    def _step_by_id(self, workflow: Workflow, step_id: str) -> WorkflowStep | None:
        for s in workflow.steps:
            if s.id == step_id:
                return s
        return None

    def _step_index(self, workflow: Workflow, step_id: str) -> int:
        for i, s in enumerate(workflow.steps):
            if s.id == step_id:
                return i
        return -1

    def _advance(self, instance: WorkflowInstance, workflow: Workflow) -> None:
        """Linear advance to the next step (DAG resolved as ordered list for v2.5.0)."""
        if instance.current_step_id is None:
            return
        idx = self._step_index(workflow, instance.current_step_id)
        if idx >= 0 and idx + 1 < len(workflow.steps):
            instance.current_step_id = workflow.steps[idx + 1].id
        else:
            instance.current_step_id = None

    def _resolve_inputs(self, step: WorkflowStep, instance: WorkflowInstance, workflow: Workflow) -> dict[str, Any]:
        """Resolve input_mapping references like 'step_1.output.bbox.x'.

        Looks up prior step results stored in instance.step_results (by step id).
        Unknown refs resolve to None (engine is tolerant; strict validation is
        a future enhancement).
        """
        params: dict[str, Any] = {}
        for key, ref in step.input_mapping.items():
            # ref format: "<step_id>.output.<field>"
            parts = ref.split(".")
            if len(parts) >= 3 and parts[1] == "output":
                src_step = parts[0]
                field = parts[2]
                val = instance.step_results.get(src_step)
                if isinstance(val, dict):
                    params[key] = val.get(field)
                else:
                    params[key] = None
            else:
                params[key] = ref
        return params

    async def _run_step(
        self, step: WorkflowStep, params: dict[str, Any], agent: Agent | None, instance: WorkflowInstance | None = None
    ) -> Artifact:
        """Execute a step: via the assigned agent (Task) or CapabilityExecutor."""
        if agent is not None and step.capability in agent.capabilities:
            task = Task(
                name=step.name,
                capability=step.capability,
                metadata=params,
                workflow_id=instance.id if instance is not None else None,
            )
            # activate the previously-dead Task.workflow_id field (ADR-019)
            return await self._agents.execute(agent.agent_id, task, workflow_id=task.workflow_id)
        return await self._caps.execute(step.capability, params)

    async def _emit(self, event: DomainEvent) -> None:
        if self._store is not None:
            await self._store.append(event)
        self._bus.publish(event)


__all__ = ["WorkflowEngine"]
