from __future__ import annotations

from codex_supervisor_bridge.backends.agent import AgentBackend
from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime, get_codex_runtime
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


class AgentPlanGateError(RuntimeError):
    """A backend-neutral Supervisor Plan Gate refusal."""


def _normalized_plan(content: str) -> str:
    return "\n".join(line.rstrip() for line in content.strip().splitlines())


class AgentExecutionCoordinator:
    """Run the Supervisor-owned plan gate against any AgentBackend implementation."""

    def __init__(self, memory: MemoryService, agent_backend: AgentBackend) -> None:
        self.memory = memory
        self.agent_backend = agent_backend

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        self.memory.assert_revision(task_id, expected_revision)
        handle = await self.agent_backend.start_plan(
            task_id=task_id,
            context_pack=context_pack,
            workspace=workspace,
        )
        bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            remote_status=handle.status,
            task_phase=TaskPhase.PLANNING,
            current_state="Codex is preparing a read-only plan.",
            event_payload={
                "mode": "plan",
                "reconciliation_required": handle.reconciliation_required,
            },
        )
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
        self.memory.assert_revision(task_id, expected_revision)
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
        handle = await self.agent_backend.start_execution(
            task_id=task_id,
            context_pack=context_pack,
            approved_plan=approved.content,
            workspace=workspace,
            lease=lease,
        )
        bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id=handle.workflow_id or plan_handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id or plan_handle.thread_id,
            turn_id=handle.turn_id,
            remote_status=handle.status,
            task_phase=TaskPhase.IMPLEMENTING,
            current_state="Codex is implementing the locally approved plan.",
            event_payload={"mode": "execute", "plan_id": approved.plan_id},
        )
        return handle
