from __future__ import annotations

from typing import NoReturn

from codex_supervisor_bridge.backends.agent import AgentBackend
from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import (
    assert_agent_safety_clear,
    latch_agent_compensation,
    record_agent_compensation_required,
    record_agent_compensation_succeeded,
)
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime, get_codex_runtime
from codex_supervisor_bridge.memory.errors import ConflictError, StaleRevisionError
from codex_supervisor_bridge.memory.execution import get_execution_state
from codex_supervisor_bridge.memory.models import (
    ActiveWriter,
    Actor,
    EventType,
    Plan,
    PlanStatus,
    TaskPhase,
)
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import get_workspace_binding
from codex_supervisor_bridge.memory.workspace_models import WorkspaceBindingStatus


class AgentPlanGateError(RuntimeError):
    """A backend-neutral Supervisor Plan Gate refusal."""


class AgentStaleContextError(AgentPlanGateError):
    """A remote operation was compensated after its local context became stale."""


class AgentCompensationRequiredError(AgentPlanGateError):
    """A remote operation may still be active and requires durable reconciliation."""


_INTERRUPT_SUCCESS = {
    "interrupted",
    "cancelled",
    "canceled",
    "stopped",
    "terminated",
    "completed",
    "succeeded",
    "success",
}


def _runtime_identity(runtime: object | None) -> tuple[str | None, ...] | None:
    if runtime is None:
        return None
    return tuple(
        getattr(runtime, field, None)
        for field in (
            "runtime_instance_id",
            "runtime_epoch",
            "workflow_id",
            "operation_id",
            "thread_id",
            "turn_id",
        )
    )


def _active_runtime(runtime: object | None) -> bool:
    if runtime is None:
        return False
    return (getattr(runtime, "remote_status", "") or "").lower() in {
        "planning",
        "executing",
        "running",
        "inprogress",
        "in_progress",
        "started",
    }


def _normalized_plan(content: str) -> str:
    return "\n".join(line.rstrip() for line in content.strip().splitlines())


class AgentExecutionCoordinator:
    """Run the Supervisor-owned plan gate against any AgentBackend implementation."""

    def __init__(self, memory: MemoryService, agent_backend: AgentBackend) -> None:
        self.memory = memory
        self.agent_backend = agent_backend

    def assert_safety_clear(self, task_id: str) -> None:
        try:
            assert_agent_safety_clear(self.memory.store, task_id)
        except ConflictError as exc:
            raise AgentPlanGateError(str(exc)) from exc
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is not None and binding.state != WorkspaceBindingStatus.ACTIVE:
            raise AgentPlanGateError(
                "Workspace requires reconciliation before starting another Codex operation"
            )

    async def compensate_remote(
        self,
        task_id: str,
        operation: str,
        handle: PlanHandle,
        stale_reason: str,
    ) -> NoReturn:
        """Interrupt a stale remote request and persist failure when it is uncertain."""
        latch_agent_compensation(
            self.memory.store,
            task_id,
            operation=operation,
            summary=(
                f"{operation} became stale; all new writes are blocked while "
                "remote compensation is pending."
            ),
            details={"stage": "INTERRUPT_PENDING", "stale_reason": stale_reason},
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )
        try:
            snapshot = await self.agent_backend.interrupt(handle)
        except Exception as exc:
            record_agent_compensation_required(
                self.memory.store,
                task_id,
                operation=operation,
                summary=(
                    f"{operation} became stale and compensation interrupt failed; "
                    "remote runtime may still be active."
                ),
                details={"stale_reason": stale_reason, "error_type": type(exc).__name__},
                workflow_id=handle.workflow_id,
                operation_id=handle.operation_id,
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
            )
            raise AgentCompensationRequiredError(
                f"COMPENSATION_REQUIRED: {operation} stale context; "
                "interrupt failed and reconciliation is required"
            ) from exc

        status = (snapshot.status or "").strip().lower()
        if snapshot.reconciliation_required or status not in _INTERRUPT_SUCCESS:
            record_agent_compensation_required(
                self.memory.store,
                task_id,
                operation=operation,
                summary=(
                    f"{operation} became stale and compensation outcome is {snapshot.status!r}; "
                    "runtime reconciliation is required."
                ),
                details={
                    "stale_reason": stale_reason,
                    "interrupt_status": snapshot.status,
                    "interrupt_reconciliation_required": snapshot.reconciliation_required,
                },
                workflow_id=handle.workflow_id,
                operation_id=handle.operation_id,
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
            )
            raise AgentCompensationRequiredError(
                f"COMPENSATION_REQUIRED: {operation} stale context; "
                "interrupt outcome is unknown and reconciliation is required"
            )

        record_agent_compensation_succeeded(
            self.memory.store,
            task_id,
            operation=operation,
            summary=f"{operation} stale remote runtime was interrupted before binding.",
            details={"stale_reason": stale_reason, "interrupt_status": snapshot.status},
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )
        raise AgentStaleContextError(
            f"STALE_CONTEXT: {operation} result was not bound because local task state changed; "
            "remote runtime was interrupted"
        )

    def post_call_stale_reasons(
        self,
        task_id: str,
        expected_revision: int,
        baseline_task: object,
        baseline_runtime: object | None,
        handle: PlanHandle,
        *,
        operation: str,
        lease: WriterLeaseToken | None = None,
        approved_plan_id: str | None = None,
        approved_content: str | None = None,
        remote_result: AgentSnapshot | None = None,
    ) -> list[str]:
        reasons: list[str] = []
        current = self.memory.get_task(task_id)
        if current.revision != expected_revision:
            reasons.append(
                f"task revision changed from {expected_revision} to {current.revision}"
            )
        for field in ("intent_version", "plan_version", "current_goal", "phase", "status"):
            if getattr(current, field) != getattr(baseline_task, field):
                reasons.append(f"task {field} changed")
        try:
            assert_agent_safety_clear(self.memory.store, task_id)
        except ConflictError:
            reasons.append("task is already blocked by agent compensation/reconciliation")

        runtime = get_codex_runtime(self.memory.store, task_id)
        if _runtime_identity(runtime) != _runtime_identity(baseline_runtime):
            reasons.append("current Codex runtime identity changed")
        execution = get_execution_state(self.memory.store, task_id)
        if lease is not None:
            if execution.active_writer != ActiveWriter.CODEX:
                reasons.append("active writer is no longer CODEX")
            if execution.writer_epoch != lease.writer_epoch:
                reasons.append("writer epoch changed")
            if lease.task_id != task_id or lease.writer != ActiveWriter.CODEX:
                reasons.append("the execution lease identity is invalid")

            approved = self.memory.approved_plan(task_id)
            if approved is None or approved.status != PlanStatus.APPROVED:
                reasons.append("the approved plan is no longer current")
            else:
                if approved.plan_id != approved_plan_id:
                    reasons.append("the approved plan was superseded")
                if approved_content is not None and _normalized_plan(approved.content) != _normalized_plan(approved_content):
                    reasons.append("the approved plan content changed")

        if handle.reconciliation_required or handle.status.strip().lower() in {
            "unknown",
            "reconciliation_required",
            "compensation_required",
        }:
            reasons.append("remote acknowledgement outcome is unknown")
        if remote_result is not None:
            status = (remote_result.status or "").strip().lower()
            if (
                remote_result.reconciliation_required
                or status in {"unknown", "reconciliation_required", "compensation_required"}
            ):
                reasons.append(f"remote {operation} outcome is {status!r}")
        return reasons

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        baseline_task = self.memory.assert_revision(task_id, expected_revision)
        self.assert_safety_clear(task_id)
        baseline_runtime = get_codex_runtime(self.memory.store, task_id)
        if _active_runtime(baseline_runtime):
            raise AgentPlanGateError("An active Codex runtime already exists for this task")
        handle = await self.agent_backend.start_plan(
            task_id=task_id,
            context_pack=context_pack,
            workspace=workspace,
        )
        reasons = self.post_call_stale_reasons(
            task_id,
            expected_revision,
            baseline_task,
            baseline_runtime,
            handle,
            operation="plan",
        )
        if reasons:
            await self.compensate_remote(task_id, "plan", handle, "; ".join(reasons))
        try:
            bind_codex_runtime(
                self.memory.store,
                task_id,
                expected_revision,
                event_type=EventType.CODEX_STARTED,
                workflow_id=handle.workflow_id,
                operation_id=handle.operation_id,
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
                runtime_instance_id=handle.runtime_instance_id,
                runtime_epoch=handle.runtime_epoch,
                runtime_ownership=handle.runtime_ownership,
                isolation_verified=handle.isolation_verified,
                interrupt_attempted=False,
                remote_status=handle.status,
                task_phase=TaskPhase.PLANNING,
                current_state="Codex is preparing a read-only plan.",
                event_payload={
                    "mode": "plan",
                    "reconciliation_required": handle.reconciliation_required,
                },
            )
        except StaleRevisionError as stale:
            await self.compensate_remote(task_id, "plan", handle, str(stale))
        return handle

    def import_plan(
        self,
        task_id: str,
        expected_revision: int,
        result: AgentSnapshot | PlanHandle | PlanResult,
    ) -> Plan:
        self.memory.assert_revision(task_id, expected_revision)
        if isinstance(result, PlanResult):
            plan_result = result
            requires_reconciliation = False
            status = plan_result.status
        elif isinstance(result, PlanHandle):
            plan_result = result.plan
            requires_reconciliation = result.reconciliation_required
            status = result.status
        else:
            plan_result = result.plan
            requires_reconciliation = False
            status = result.status
        if requires_reconciliation or status.upper() == "UNKNOWN":
            raise AgentPlanGateError(
                "Cannot import a plan while the agent outcome requires reconciliation"
            )
        if plan_result is None or not plan_result.content.strip():
            raise AgentPlanGateError("Agent returned no usable read-only plan")
        if plan_result.status.lower() in {"invalid", "failed", "error"}:
            raise AgentPlanGateError("Agent returned an invalid read-only plan")
        return self.memory.store.create_plan(
            task_id,
            expected_revision,
            plan_result.content,
            actor=Actor.CODEX,
        )

    async def start_execution(
        self,
        task_id: str,
        expected_revision: int,
        *,
        plan_id: str,
        plan_handle: PlanHandle,
        plan_result: PlanResult,
        context_pack: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        baseline_task = self.memory.assert_revision(task_id, expected_revision)
        self.assert_safety_clear(task_id)
        execution = get_execution_state(self.memory.store, task_id)
        if execution.active_writer != ActiveWriter.CODEX:
            raise AgentPlanGateError("Codex execution requires an active CODEX writer lease")
        if (
            lease.task_id != task_id
            or lease.writer != ActiveWriter.CODEX
            or lease.writer_epoch != execution.writer_epoch
            or lease.task_revision != expected_revision
        ):
            raise AgentPlanGateError("Codex execution lease is stale")
        approved = self.memory.approved_plan(task_id)
        if approved is None or approved.status != PlanStatus.APPROVED:
            raise AgentPlanGateError("No locally APPROVED plan exists")
        if approved.plan_id != plan_id:
            raise AgentPlanGateError("The requested plan is stale or superseded")
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise AgentPlanGateError("No active read-only plan runtime exists")
        if (
            runtime.workflow_id
            and plan_handle.workflow_id
            and runtime.workflow_id != plan_handle.workflow_id
        ):
            raise AgentPlanGateError("The read-only plan runtime is stale")
        if runtime.thread_id and plan_handle.thread_id and runtime.thread_id != plan_handle.thread_id:
            raise AgentPlanGateError("The read-only plan runtime is stale")
        if (
            runtime.runtime_instance_id != plan_handle.runtime_instance_id
            or runtime.runtime_epoch != plan_handle.runtime_epoch
        ):
            raise AgentPlanGateError(
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED: plan belongs to a prior runtime"
            )
        if plan_handle.reconciliation_required or plan_handle.status.upper() == "UNKNOWN":
            raise AgentPlanGateError(
                "Cannot execute while the read-only plan outcome requires reconciliation"
            )
        if not plan_result.content.strip():
            raise AgentPlanGateError("Execution requires the reviewed read-only plan result")
        if plan_result.status.lower() in {"invalid", "failed", "error"}:
            raise AgentPlanGateError("Execution requires a valid read-only plan result")
        if _normalized_plan(plan_result.content) != _normalized_plan(approved.content):
            raise AgentPlanGateError("The reviewed plan no longer matches the locally approved plan")
        baseline_runtime = runtime
        handle = await self.agent_backend.start_execution(
            task_id=task_id,
            context_pack=context_pack,
            approved_plan=approved.content,
            workspace=workspace,
            lease=lease,
        )
        reasons = self.post_call_stale_reasons(
            task_id,
            expected_revision,
            baseline_task,
            baseline_runtime,
            handle,
            operation="execution",
            lease=lease,
            approved_plan_id=approved.plan_id,
            approved_content=approved.content,
        )
        if reasons:
            await self.compensate_remote(task_id, "execution", handle, "; ".join(reasons))
        try:
            bind_codex_runtime(
                self.memory.store,
                task_id,
                expected_revision,
                event_type=EventType.CODEX_STARTED,
                workflow_id=handle.workflow_id or plan_handle.workflow_id,
                operation_id=handle.operation_id,
                thread_id=handle.thread_id or plan_handle.thread_id,
                turn_id=handle.turn_id,
                runtime_instance_id=handle.runtime_instance_id,
                runtime_epoch=handle.runtime_epoch,
                runtime_ownership=handle.runtime_ownership,
                isolation_verified=handle.isolation_verified,
                interrupt_attempted=False,
                remote_status=handle.status,
                task_phase=TaskPhase.IMPLEMENTING,
                current_state="Codex is implementing the locally approved plan.",
                event_payload={"mode": "execute", "plan_id": approved.plan_id},
            )
        except StaleRevisionError as stale:
            await self.compensate_remote(task_id, "execution", handle, str(stale))
        return handle
