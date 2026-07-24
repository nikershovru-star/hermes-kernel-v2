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
import uuid
from datetime import datetime, timezone
from typing import Any

from kernel.agent import BaseAgent
from kernel.capability import Capability, CapabilityExecutor
from kernel.capability_guard import (  # ADR-028 (type only)
    CapabilityGuard,
    PermissionDeniedError,
    ResourceLimitExceededError,
)
from kernel.domain import (
    Artifact,
    DeadLetterEntry,
    ExecutionOutcome,
    Plan,
    PlanStep,
    PlanStatus,
    ReplanTrigger,
    RiskLevel,
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
    ReplanTriggered,
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
        swarm_coordinator: "SwarmCoordinator | None" = None,
        dynamic_planner: "DynamicPlanner | None" = None,
        knowledge_graph: "KnowledgeGraphEngine | None" = None,
        marketplace: "PluginMarketplace | None" = None,
        observability: "ObservabilityEngine | None" = None,
        guard: "CapabilityGuard | None" = None,
        mcp: "McpGateway | None" = None,
    ) -> None:
        self._agents = agent_runtime
        self._caps = capability_executor
        self._bus = event_bus
        self._store = event_store
        self._sandbox = sandbox
        self._health = health_monitor
        self._dlq = dead_letter
        self._swarm = swarm_coordinator
        self._planner = dynamic_planner
        self._kg = knowledge_graph
        self._mp = marketplace
        self._obs = observability
        self._guard = guard  # ADR-028: optional CapabilityGuard
        self._mcp = mcp  # ADR-029: optional McpGateway
        self._instances: dict[str, WorkflowInstance] = {}

    # -- adaptive execution (ADR-024) ------------------------------------ #
    def _set_planner(self, dynamic_planner: "DynamicPlanner | None") -> None:
        self._planner = dynamic_planner

    async def execute_adaptive(
        self, instance_id: str, workflow: Workflow
    ) -> list[Artifact]:
        span_id = None
        if self._obs is not None:
            span_id = await self._obs.start_span(instance_id, "execute_adaptive", correlation_id=instance_id)
        try:
            result = await self._execute_adaptive_inner(instance_id, workflow)
            if self._obs is not None:
                await self._obs.record_metric(
                    "wf.executions", 1.0,
                    labels={"workflow_id": workflow.id, "instance_id": instance_id},
                )
                await self._obs.record_metric(
                    "wf.steps_total", float(len(workflow.steps)),
                    labels={"workflow_id": workflow.id},
                )
                errs = sum(1 for a in result if getattr(a, "type", None) in ("error", "approval_required"))
                if errs:
                    await self._obs.record_metric("wf.errors", float(errs), labels={"workflow_id": workflow.id})
                    await self._obs.log("error", f"workflow {workflow.id} completed with {errs} failed step(s)", correlation_id=instance_id, context={"workflow_id": workflow.id})
                else:
                    await self._obs.log("info", f"workflow executed: {workflow.id}", correlation_id=instance_id, context={"steps": len(workflow.steps)})
            return result
        except Exception as exc:  # noqa: BLE001
            if self._obs is not None:
                await self._obs.log("error", f"workflow execution failed: {exc}", correlation_id=instance_id, context={"workflow_id": workflow.id})
                await self._obs.record_metric("wf.errors", 1.0, labels={"workflow_id": workflow.id})
            raise
        finally:
            if span_id is not None:
                await self._obs.finish_span(span_id, status="ok")

    async def _execute_adaptive_inner(
        self, instance_id: str, workflow: Workflow
    ) -> list[Artifact]:
        if self._planner is None:
            # backward-compatible fallback: linear execute via execute_step
            inst = self.get_instance(instance_id)
            artifacts: list[Artifact] = []
            while inst.status == WorkflowStatus.RUNNING and inst.current_step_id is not None:
                artifacts.append(await self.execute_step(inst, workflow))
            return artifacts
        inst = self.get_instance(instance_id)
        plan_steps = [
            PlanStep(
                step_id=s.id,
                capability=s.capability,
                agent_id=None,
                dependencies=[],
                estimated_duration_ms=1000,
                risk=RiskLevel.LOW,
                retry_budget=3,
            )
            for s in workflow.steps
        ]
        plan = await self._planner.create_plan(workflow.id, plan_steps)

        def make_executor(inst: WorkflowInstance, wf: Workflow):
            async def _exec(step: PlanStep) -> ExecutionOutcome:
                # reuse the existing local execution path (execute_step)
                artifact = await self.execute_step(inst, wf)
                status = "success" if artifact.type not in ("error", "approval_required") else (
                    "cancelled" if artifact.type == "approval_required" else "failure"
                )
                return ExecutionOutcome(
                    outcome_id=uuid.uuid4().hex,
                    plan_id=plan.plan_id,
                    step_id=step.step_id,
                    status=status,
                    duration_ms=1,
                    retry_count=0,
                )
            return _exec

        executor = make_executor(inst, workflow)
        await self._planner.execute_plan(plan.plan_id, executor)
        # collect artifacts produced (re-run is idempotent for returned shape check)
        return [Artifact(type="plan_executed", content={"plan_id": plan.plan_id, "status": plan.status.value}, format="json")]

    async def replan_step(self, instance_id: str, step_id: str, reason: str) -> Plan | None:
        inst = self.get_instance(instance_id)
        if self._planner is None:
            return None
        workflow_id = inst.workflow_id
        trigger = ReplanTrigger(
            trigger_id=uuid.uuid4().hex,
            plan_id=workflow_id,
            reason=reason,
            context={"step_id": step_id, "instance_id": instance_id},
        )
        # emit ReplanTriggered on the planner's bus if wired
        if self._planner._bus is not None:
            self._planner._bus.publish(
                ReplanTriggered(trigger.trigger_id, workflow_id, reason, step_id)
            )
        if self._planner._store is not None:
            await self._planner._store.append(
                ReplanTriggered(trigger.trigger_id, workflow_id, reason, step_id)
            )
        plan = await self._planner._replan(
            Plan(plan_id=workflow_id, workflow_id=workflow_id, status=PlanStatus.DRAFT, steps=[]),
            trigger,
        )
        return plan

    async def execute_with_context(
        self, instance_id: str, workflow: Workflow, context_graph_id: str | None = None
    ) -> list[Artifact]:
        """Execute a workflow, first stamping KG entity_ids matching workflow keywords.

        Before running, queries the configured ``knowledge_graph`` (or the graph
        given by ``context_graph_id``) for entities whose name matches any
        workflow step capability/name token, and records the matched entity_ids
        into ``workflow.context["kg_matches"]``. Then delegates to
        ``execute_adaptive`` (falling back to legacy ``execute_step`` when no
        planner is wired). If no knowledge graph is configured, runs unchanged.
        """
        if self._kg is not None and context_graph_id is not None:
            matched: list[str] = []
            from kernel.semantic_graph import GraphQuery

            keywords = " ".join(
                [workflow.name] + [s.capability for s in workflow.steps] + [s.name for s in workflow.steps]
            ).lower()
            g = self._kg.get_graph(context_graph_id)
            if g is not None:
                for e in g.entities.values():
                    if e.name.lower() in keywords or any(tok in e.name.lower() for tok in keywords.split()):
                        matched.append(e.entity_id)
            workflow.context.setdefault("kg_matches", []).extend(matched)
        # reuse the adaptive/fallback path
        span_id = None
        if self._obs is not None:
            span_id = await self._obs.start_span(instance_id, "execute_with_context", parent_id=instance_id, correlation_id=instance_id)
            if matched := workflow.context.get("kg_matches"):
                await self._obs.record_metric("wf.kg_matches", float(len(matched)), labels={"workflow_id": workflow.id})
        result = await self.execute_adaptive(instance_id, workflow)
        if span_id is not None:
            await self._obs.finish_span(span_id, status="ok")
        return result

    async def discover_plugins(self, capability_query: str) -> list[PluginPackage]:
        """Find installed/available packages providing ``capability_query``.

        Requires ``marketplace`` wired. Searches installed packages first, then
        available catalog, matching by substring (case-insensitive) on the
        capability name. Returns the matching ``PluginPackage`` list.
        """
        if self._mp is None:
            return []
        q = capability_query.lower()
        candidates = self._mp.list_installed() + self._mp.list_available()
        seen: set[str] = set()
        matched: list[PluginPackage] = []
        for pkg in candidates:
            if pkg.package_id in seen:
                continue
            if any(q in cap.lower() for cap in pkg.capabilities):
                seen.add(pkg.package_id)
                matched.append(pkg)
        # ADR-028: when a guard is wired, drop packages the caller is not
        # permitted to discover (check(agent_id="discover" path via caller).
        if self._guard is not None:
            filtered = [p for p in matched if self._guard.check(p.package_id, "discover", f"plugin:{p.package_id}")]
            matched = filtered
        return matched

    def _step_package_id(self, capability: str) -> "str | None":
        """Resolve which installed plugin package backs ``capability`` (ADR-028).

        Returns package_id, or None for built-in / non-plugin capabilities.
        """
        if self._mp is None:
            return None
        for pkg in self._mp.list_installed():
            for cap in pkg.capabilities:
                if cap == capability or capability.startswith(cap + "."):
                    return pkg.package_id
        return None

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
        if self._obs is not None:
            await self._obs.log("info", f"workflow started: {workflow.id}", correlation_id=inst.id, context={"steps": len(workflow.steps)})
        return inst

    async def _observe_span(self, trace_id: str, span_name: str, correlation_id: str | None = None):
        """Context-manager-free span helper; returns the span_id and finishes lazily."""
        if self._obs is None:
            return None
        return await self._obs.start_span(trace_id, span_name, correlation_id=correlation_id)

    def get_instance(self, instance_id: str) -> WorkflowInstance:
        inst = self._instances.get(instance_id)
        if inst is None:
            raise KeyError(f"workflow instance {instance_id!r} not found")
        return inst

    async def get_status(self, instance_id: str) -> WorkflowInstance:
        return self.get_instance(instance_id)

    # -- swarm-aware scheduling (ADR-023) -- #
    def schedule_swarm(self, swarm_id: str, tasks: list[Task], from_agent: str) -> list:
        """Delegate a batch of tasks to swarm members via the coordinator.

        Requires a ``swarm_coordinator`` (otherwise raises RuntimeError). Each
        task is matched to an eligible member (capability-aware, lowest-load,
        round-robin) and a ``TaskDelegation`` is returned. The local execution
        path of ``execute_step`` is untouched — this is an alternative entry point
        the WorkflowEngine exposes for distributed orchestration.
        """
        if self._swarm is None:
            raise RuntimeError("no swarm_coordinator configured")
        return [self._swarm.delegate_task(swarm_id, t, from_agent) for t in tasks]

    async def execute_step_swarm(self, swarm_id: str, instance: WorkflowInstance, workflow: Workflow, from_agent: str) -> Artifact:
        """Like ``execute_step`` but routes the current step to a swarm member."""
        delegation = self.schedule_swarm(swarm_id, [self._current_step_task(instance, workflow)], from_agent)[0]
        self._swarm.complete_delegation(delegation.delegation_id, result_summary="swarm step")
        return Artifact(type="task", content={"delegated_to": delegation.to_agent, "task_id": delegation.task_id}, format="json")

    def _current_step_task(self, instance: WorkflowInstance, workflow: Workflow) -> Task:
        step = self._current_step(workflow, instance)
        return Task(name=step.id, capability=step.capability)

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

        # ADR-029: an "mcp:*" step with no gateway wired fails deterministically
        # (WorkflowStepFailed reason "mcp_not_wired", instance -> FAILED) — the
        # same guard-style honest failure as ADR-028, never a silent fallback.
        is_mcp_step = bool(step.capability) and step.capability.startswith("mcp:")
        if is_mcp_step and self._mcp is None:
            instance.status = WorkflowStatus.FAILED
            await self._emit(
                WorkflowStepFailed(instance.id, step.id, "mcp_not_wired", attempt, False)
            )
            return Artifact(
                type="error",
                content={"reason": "mcp_not_wired", "capability": step.capability},
                format="json",
            )

        # ADR-028: cooperatively guard plugin-backed capabilities. The step
        # runner is wrapped so denials / resource breaches raise INSIDE the
        # guard (full audit trail). We convert them to a clean error artifact
        # and FAIL the workflow instead of crashing the engine. The prior
        # pre-check left status=RUNNING on denial, which made the linear
        # fallback (no planner) loop forever — honest fix.
        guarded_pkg: str | None = None
        if self._guard is not None and step.capability:
            guarded_pkg = self._step_package_id(step.capability)

        try:
            started = time.monotonic()
            if guarded_pkg is not None:
                async with self._guard.wrap(
                    lambda: self._run_step(step, params, agent, instance),
                    guarded_pkg,
                    action="execute",
                    resource=f"capability:{step.capability}",
                ) as coro:
                    artifact = await coro
            elif self._sandbox is None:
                artifact = await self._run_step(step, params, agent, instance)
            else:
                policy = policy or self._resolve_policy(step, workflow)
                artifact = await self._sandbox.run(
                    self._run_step(step, params, agent, instance),
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
        except (PermissionDeniedError, ResourceLimitExceededError) as denied:
            instance.context.setdefault("permission_denied", []).append(
                {"step_id": step.id, "capability": step.capability, "package_id": guarded_pkg}
            )
            instance.status = WorkflowStatus.FAILED
            await self._emit(WorkflowStepFailed(instance.id, step.id, f"guard denied: {step.capability}", attempt=1, will_retry=False))
            return Artifact(
                type="error",
                content={"reason": "permission_denied", "capability": step.capability, "package_id": guarded_pkg},
                format="json",
                source=f"guard:{guarded_pkg}",
            )
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
        """Execute a step: via MCP gateway (ADR-029), the assigned agent (Task) or CapabilityExecutor."""
        if step.capability and step.capability.startswith("mcp:") and self._mcp is not None:
            return await self._run_mcp_step(step, params, instance)
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

    # -- MCP gateway integration (ADR-029) --------------------------------- #
    async def _run_mcp_step(
        self, step: WorkflowStep, params: dict[str, Any], instance: WorkflowInstance | None
    ) -> Artifact:
        """Run an ``mcp:*`` step through the gateway; record latency in context."""
        import time as _time

        capability = step.capability or ""
        body = capability[4:]
        if "::" in body:
            server_url, tool_name = body.split("::", 1)
        else:
            tool = self._mcp.resolve_capability(capability)
            if tool is None:
                raise KeyError(f"no MCP tool resolves capability '{capability}'")
            server_url, tool_name = tool.server_url, tool.name
        started = _time.monotonic()
        artifact = await self._mcp.call_tool(server_url, tool_name, dict(params))
        latency_ms = (_time.monotonic() - started) * 1000.0
        if instance is not None:
            instance.context["mcp_latency_ms"] = latency_ms
        return artifact


__all__ = ["WorkflowEngine"]
