from __future__ import annotations

from typing import Any, Protocol

from codex_supervisor_bridge.backends.models import (
    GitState,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import (
    record_agent_compensation_required,
    record_agent_compensation_succeeded,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeState,
    bind_codex_runtime,
    get_codex_runtime,
)
from codex_supervisor_bridge.memory.execution import get_execution_state
from codex_supervisor_bridge.memory.models import (
    ActiveWriter,
    ContextPackMode,
    EventType,
    TaskPhase,
)
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import get_workspace_binding

from .agent_execution import (
    AgentCompensationRequiredError,
    AgentExecutionCoordinator,
    AgentPlanGateError,
)
from .agent_session import AgentSessionManager

SEMANTIC_TOOLS = [
    "get_codex_control_health",
    "get_codex_runtime_capabilities",
    "preflight_codex_project",
    "start_codex_plan",
    "get_codex_status",
    "import_codex_plan",
    "execute_codex_approved_plan",
    "soft_steer_codex",
    "interrupt_codex",
    "list_codex_pending_interactions",
    "answer_codex_pending_interaction",
]

_ACTIVE_STATUSES = {
    "planning",
    "executing",
    "running",
    "inprogress",
    "in_progress",
    "started",
}

_COMPLETE_STATUSES = {
    "completed",
    "interrupted",
    "cancelled",
    "canceled",
    "stopped",
    "succeeded",
    "success",
    "failed",
    "error",
}

_MUTATION_INTERACTION_KINDS = {
    "command_approval",
    "file_change_approval",
    "permissions_approval",
}


def _runtime_identity(runtime: CodexRuntimeState | None) -> tuple[str | None, ...] | None:
    if runtime is None:
        return None
    return tuple(
        getattr(runtime, field, None)
        for field in ("workflow_id", "operation_id", "thread_id", "turn_id")
    )


class CodexSemanticFacade(Protocol):
    async def health(self) -> dict[str, Any]: ...

    async def runtime_capabilities(self) -> dict[str, Any]: ...

    async def preflight(
        self,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]: ...

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        project_id: str,
        cwd: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]: ...

    async def status(self, task_id: str) -> dict[str, Any]: ...

    async def import_latest_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    async def execute_approved_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]: ...

    async def soft_steer(
        self,
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> dict[str, Any]: ...

    async def interrupt(
        self,
        task_id: str,
        expected_revision: int,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]: ...

    async def pending_interactions(self, task_id: str) -> dict[str, Any]: ...

    async def answer_interaction(
        self,
        task_id: str,
        expected_revision: int,
        interaction_id: str,
        *,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]: ...


class CodexCoordinatorFacade:
    """Compatibility facade keeping the legacy Control Plane path on the same MCP surface."""

    def __init__(
        self,
        coordinator: Any,
        *,
        profile: str = "existing",
        workspace_backend: str = "kandev",
        agent_backend: str = "control_plane",
    ) -> None:
        self.coordinator = coordinator
        self.profile = profile
        self.workspace_backend = workspace_backend
        self.agent_backend = agent_backend

    async def health(self) -> dict[str, Any]:
        payload = await self.coordinator.health()
        return {**payload, "profile": self.profile}

    async def runtime_capabilities(self) -> dict[str, Any]:
        payload = await self.coordinator.runtime_capabilities()
        return {**payload, "profile": self.profile}

    async def preflight(
        self,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]:
        return await self.coordinator.preflight(
            project_id=project_id,
            cwd=cwd,
            model=model,
            workflow_kind=workflow_kind,
        )

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        project_id: str,
        cwd: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        return await self.coordinator.start_plan(
            task_id,
            expected_revision,
            project_id=project_id,
            cwd=cwd,
            model=model,
        )

    async def status(self, task_id: str) -> dict[str, Any]:
        return await self.coordinator.status(task_id)

    async def import_latest_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        return await self.coordinator.import_latest_plan(task_id, expected_revision)

    async def execute_approved_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        return await self.coordinator.execute_approved_plan(
            task_id,
            expected_revision,
            sandbox="workspace-write",
        )

    async def soft_steer(
        self,
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> dict[str, Any]:
        return await self.coordinator.soft_steer(
            task_id,
            expected_revision,
            instruction,
        )

    async def interrupt(
        self,
        task_id: str,
        expected_revision: int,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        return await self.coordinator.interrupt(
            task_id,
            expected_revision,
            reason=reason,
        )

    async def pending_interactions(self, task_id: str) -> dict[str, Any]:
        return await self.coordinator.pending_interactions(task_id)

    async def answer_interaction(
        self,
        task_id: str,
        expected_revision: int,
        interaction_id: str,
        *,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        return await self.coordinator.answer_interaction(
            task_id,
            expected_revision,
            interaction_id,
            decision=decision,
            answers=answers,
            scope=scope,
        )


class AgentSupervisorFacade:
    """Provider-neutral semantic control surface over any AgentBackend."""

    def __init__(
        self,
        memory: MemoryService,
        coordinator: AgentExecutionCoordinator,
        *,
        profile: str,
        workspace_backend: str,
        agent_backend: str,
        session_manager: AgentSessionManager | None = None,
    ) -> None:
        self.memory = memory
        self.coordinator = coordinator
        self.profile = profile
        self.workspace_backend = workspace_backend
        self.agent_backend = agent_backend
        self.session_manager = session_manager

    @property
    def checkpoint_backend(self) -> Any:
        if self.session_manager is not None:
            return self.session_manager
        return self.coordinator.agent_backend

    def _ensure_task_binding(self, task_id: str, expected_revision: int) -> Any:
        from codex_supervisor_bridge.memory.backend_binding import (
            assert_task_backend_binding,
            bind_task_backend,
        )

        try:
            existing = assert_task_backend_binding(
                self.memory.store,
                task_id,
                workspace_backend=self.workspace_backend,
                agent_backend=self.agent_backend,
            )
        except Exception as exc:
            raise AgentPlanGateError(
                "Task is already bound to a different development profile; "
                "restart the Bridge with that profile or migrate explicitly"
            ) from exc
        if existing is not None:
            return None
        bind_task_backend(
            self.memory.store,
            task_id,
            expected_revision,
            workspace_backend=self.workspace_backend,
            agent_backend=self.agent_backend,
            profile=self.profile,
        )
        return None

    async def _agent(self) -> Any:
        if self.session_manager is not None:
            return self.session_manager
        return self.coordinator.agent_backend

    def _workspace(
        self,
        task_id: str,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
    ) -> WorkspaceState:
        task = self.memory.get_task(task_id)
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is not None:
            return WorkspaceState(
                workspace_id=binding.workspace_id,
                repository=binding.repository,
                root=binding.root,
                git=GitState(
                    branch=binding.git_branch,
                    head=binding.git_head,
                    dirty=binding.dirty,
                    changed_files=binding.changed_files,
                ),
            )
        root = cwd or project_id or task.repository
        if not root:
            raise AgentPlanGateError("Task has no canonical workspace binding or project path")
        return WorkspaceState(
            workspace_id=project_id or root,
            repository=task.repository or root,
            root=root,
        )

    @staticmethod
    def _runtime_handle(runtime: CodexRuntimeState) -> PlanHandle:
        return PlanHandle(
            operation_id=runtime.operation_id,
            workflow_id=runtime.workflow_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            status=runtime.remote_status or "unknown",
        )

    async def health(self) -> dict[str, Any]:
        agent = await self._agent()
        health = await agent.health()
        return {
            "ok": health.status.value == "READY",
            "status": health.status.value,
            "user_message": health.user_message,
            "repairable": health.repairable,
            "profile": self.profile,
            "workspace_backend": self.workspace_backend,
            "agent_backend": self.agent_backend,
            "session": self.session_manager.status() if self.session_manager else None,
        }

    async def runtime_capabilities(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "workspace_backend": self.workspace_backend,
            "agent_backend": self.agent_backend,
            "semantic_tools": list(SEMANTIC_TOOLS),
            "single_writer": True,
            "plan_gated_execution": True,
            "backend_binding_required": True,
            "restart_recovery": "fail_closed_reconciliation",
        }

    async def preflight(
        self,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]:
        if workflow_kind not in {"plan", "write", "review"}:
            raise AgentPlanGateError("workflow_kind must be plan, write, or review")
        health = await self.health()
        project = cwd or project_id
        return {
            "workflow_kind": workflow_kind,
            "project": project,
            "model": model,
            "checks": {
                "agent_ready": bool(health["ok"]),
                "workspace_path_available": bool(project),
                "plan_gate_enforced": True,
            },
            "ready": bool(health["ok"]) and bool(project),
        }

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        project_id: str,
        cwd: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        del model
        task = self.memory.assert_revision(task_id, expected_revision)
        self._ensure_task_binding(task_id, task.revision)
        task = self.memory.get_task(task_id)
        workspace = self._workspace(task_id, project_id=project_id, cwd=cwd)
        pack = self.memory.get_context_pack(task_id, mode=ContextPackMode.PLAN_REVIEW)
        handle = await self.coordinator.start_plan(
            task_id,
            task.revision,
            context_pack=pack.content,
            workspace=workspace,
        )
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "runtime": get_codex_runtime(self.memory.store, task_id).model_dump(mode="json"),
            "plan_handle": handle.model_dump(mode="json"),
            "profile": self.profile,
        }

    async def status(self, task_id: str) -> dict[str, Any]:
        task = self.memory.get_task(task_id)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            return {"task": task.model_dump(mode="json"), "runtime": None, "snapshot": None, "pollRecommended": False}
        agent = await self._agent()
        snapshot = await agent.observe(self._runtime_handle(runtime))
        status = (snapshot.status or "").strip().lower()
        poll_recommended = (
            not snapshot.reconciliation_required
            and status not in _COMPLETE_STATUSES
            and status not in {"unknown", "reconciliation_required", "compensation_required"}
        )
        return {
            "task": task.model_dump(mode="json"),
            "runtime": runtime.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
            "pollRecommended": poll_recommended,
        }

    async def import_latest_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        task = self.memory.assert_revision(task_id, expected_revision)
        self._ensure_task_binding(task_id, task.revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise AgentPlanGateError("Task has no active Codex plan workflow")
        agent = await self._agent()
        handle = self._runtime_handle(runtime)
        snapshot = await agent.observe(handle)
        plan = self.coordinator.import_plan(task_id, expected_revision, snapshot)
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "plan_handle": handle.model_dump(mode="json"),
            "profile": self.profile,
        }

    async def execute_approved_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        task = self.memory.assert_revision(task_id, expected_revision)
        self._ensure_task_binding(task_id, task.revision)
        execution = get_execution_state(self.memory.store, task_id)
        if execution.active_writer != ActiveWriter.CODEX:
            raise AgentPlanGateError(
                "Codex workspace mutation requires an active CODEX writer lease; hand off first"
            )
        approved = self.memory.approved_plan(task_id)
        if approved is None or approved.status.value != "APPROVED":
            raise AgentPlanGateError("No locally APPROVED plan exists")
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise AgentPlanGateError("Task has no Codex plan runtime to execute")
        handle = self._runtime_handle(runtime)
        pack = self.memory.get_context_pack(task_id)
        execution_handle = await self.coordinator.start_execution(
            task_id,
            expected_revision,
            plan_id=approved.plan_id,
            plan_handle=handle,
            plan_result=PlanResult(content=approved.content, status="ready"),
            context_pack=pack.content,
            workspace=self._workspace(task_id),
            lease=WriterLeaseToken(
                task_id=task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=execution.writer_epoch,
                task_revision=expected_revision,
            ),
        )
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "runtime": get_codex_runtime(self.memory.store, task_id).model_dump(mode="json"),
            "plan_handle": execution_handle.model_dump(mode="json"),
            "profile": self.profile,
        }

    async def soft_steer(
        self,
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> dict[str, Any]:
        if not instruction.strip():
            raise AgentPlanGateError("instruction must not be empty")
        baseline_task = self.memory.assert_revision(task_id, expected_revision)
        execution = get_execution_state(self.memory.store, task_id)
        if execution.active_writer != ActiveWriter.CODEX:
            raise AgentPlanGateError("Codex steering requires an active CODEX writer lease")
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not any(
            [runtime.workflow_id, runtime.operation_id, runtime.thread_id, runtime.turn_id]
        ):
            raise AgentPlanGateError("Task has no active Codex runtime identity")
        self.coordinator.assert_safety_clear(task_id)
        lease = WriterLeaseToken(
            task_id=task_id,
            writer=ActiveWriter.CODEX,
            writer_epoch=execution.writer_epoch,
            task_revision=expected_revision,
        )
        approved = self.memory.approved_plan(task_id)
        approved_plan_id = (
            approved.plan_id
            if approved is not None and approved.status.value == "APPROVED"
            else None
        )
        approved_content = (
            approved.content
            if approved is not None and approved.status.value == "APPROVED"
            else None
        )
        agent = await self._agent()
        handle = self._runtime_handle(runtime)
        snapshot = await agent.steer(
            handle,
            instruction.strip(),
            lease=lease,
        )
        reasons = self.coordinator.post_call_stale_reasons(
            task_id,
            expected_revision,
            baseline_task,
            runtime,
            handle,
            operation="steer",
            lease=lease,
            approved_plan_id=approved_plan_id,
            approved_content=approved_content,
            remote_result=snapshot,
        )
        if reasons:
            await self.coordinator.compensate_remote(
                task_id,
                "steer",
                handle,
                "; ".join(reasons),
            )
        _, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STEERED,
            workflow_id=runtime.workflow_id,
            operation_id=runtime.operation_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            remote_status=snapshot.status or runtime.remote_status,
            current_state="Codex accepted a supervisor steering instruction.",
            event_payload={"instruction": instruction.strip()},
        )
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
        }

    async def interrupt(
        self,
        task_id: str,
        expected_revision: int,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.memory.assert_revision(task_id, expected_revision)
        baseline_runtime = get_codex_runtime(self.memory.store, task_id)
        if baseline_runtime is None or not any(
            [
                baseline_runtime.workflow_id,
                baseline_runtime.operation_id,
                baseline_runtime.thread_id,
                baseline_runtime.turn_id,
            ]
        ):
            raise AgentPlanGateError("Task has no Codex runtime to interrupt")
        agent = await self._agent()
        baseline_handle = self._runtime_handle(baseline_runtime)
        snapshot = await agent.interrupt(baseline_handle)
        interrupt_status = (snapshot.status or "").strip().lower()
        if snapshot.reconciliation_required or interrupt_status in {
            "unknown",
            "reconciliation_required",
            "compensation_required",
        }:
            await self._latch_interrupt_reconciliation(
                task_id,
                baseline_runtime,
                get_codex_runtime(self.memory.store, task_id),
                f"interrupt returned unknown outcome {snapshot.status!r}",
            )
            raise AgentCompensationRequiredError(
                "COMPENSATION_REQUIRED: interrupt outcome is unknown; "
                "reconciliation is required before new Codex work"
            )

        current_runtime = get_codex_runtime(self.memory.store, task_id)
        if _runtime_identity(current_runtime) != _runtime_identity(baseline_runtime):
            execution = get_execution_state(self.memory.store, task_id)
            current_status = (current_runtime.remote_status or "").strip().lower() if current_runtime else ""
            if (
                current_runtime is not None
                and current_status in _ACTIVE_STATUSES
                and execution.active_writer == ActiveWriter.CODEX
            ):
                current_handle = self._runtime_handle(current_runtime)
                try:
                    current_snapshot = await agent.observe(current_handle)
                except Exception as exc:
                    await self._latch_interrupt_reconciliation(
                        task_id,
                        baseline_runtime,
                        current_runtime,
                        f"stale interrupt; current runtime observe failed: {type(exc).__name__}",
                    )
                    raise AgentCompensationRequiredError(
                        "COMPENSATION_REQUIRED: stale interrupt cannot confirm the current runtime"
                    ) from exc
                if (
                    current_snapshot.reconciliation_required
                    or (current_snapshot.status or "").strip().lower()
                    in {"unknown", "reconciliation_required", "compensation_required"}
                ):
                    await self._latch_interrupt_reconciliation(
                        task_id,
                        baseline_runtime,
                        current_runtime,
                        f"stale interrupt; current runtime returned {current_snapshot.status!r}",
                    )
                    raise AgentCompensationRequiredError(
                        "COMPENSATION_REQUIRED: stale interrupt cannot confirm the current runtime"
                    )
                record_agent_compensation_succeeded(
                    self.memory.store,
                    task_id,
                    operation="interrupt",
                    summary="A stale Codex runtime was interrupted; the current runtime remains supervised.",
                    details={
                        "interrupt_status": snapshot.status,
                        "current_runtime_status": current_snapshot.status,
                    },
                    workflow_id=baseline_runtime.workflow_id,
                    operation_id=baseline_runtime.operation_id,
                    thread_id=baseline_runtime.thread_id,
                    turn_id=baseline_runtime.turn_id,
                )
                return {
                    "task": self.memory.get_task(task_id).model_dump(mode="json"),
                    "runtime": current_runtime.model_dump(mode="json"),
                    "snapshot": current_snapshot.model_dump(mode="json"),
                    "stale_runtime_interrupted": True,
                    "profile": self.profile,
                }
            record_agent_compensation_succeeded(
                self.memory.store,
                task_id,
                operation="interrupt",
                summary="A stale Codex runtime was interrupted before its result could bind.",
                details={"interrupt_status": snapshot.status},
                workflow_id=baseline_runtime.workflow_id,
                operation_id=baseline_runtime.operation_id,
                thread_id=baseline_runtime.thread_id,
                turn_id=baseline_runtime.turn_id,
            )
            return {
                "task": self.memory.get_task(task_id).model_dump(mode="json"),
                "runtime": (
                    current_runtime.model_dump(mode="json")
                    if current_runtime is not None
                    else None
                ),
                "snapshot": snapshot.model_dump(mode="json"),
                "stale_runtime_interrupted": True,
                "profile": self.profile,
            }

        current_task = self.memory.get_task(task_id)
        if current_task.revision != expected_revision:
            if current_runtime is None:
                await self._latch_interrupt_reconciliation(
                    task_id,
                    baseline_runtime,
                    None,
                    "revision changed during interrupt and no current runtime is persisted",
                )
                raise AgentCompensationRequiredError(
                    "COMPENSATION_REQUIRED: revision changed during interrupt "
                    "and the current runtime cannot be confirmed"
                )
            current_handle = self._runtime_handle(current_runtime)
            try:
                current_snapshot = await agent.observe(current_handle)
            except Exception as exc:
                await self._latch_interrupt_reconciliation(
                    task_id,
                    baseline_runtime,
                    current_runtime,
                    f"revision-only interrupt race; observe failed: {type(exc).__name__}",
                )
                raise AgentCompensationRequiredError(
                    "COMPENSATION_REQUIRED: revision changed during interrupt "
                    "and the runtime cannot be observed"
                ) from exc
            current_status = (current_snapshot.status or "").strip().lower()
            if (
                current_snapshot.reconciliation_required
                or current_status in {
                    "unknown",
                    "reconciliation_required",
                    "compensation_required",
                }
                or current_status in _ACTIVE_STATUSES
            ):
                await self._latch_interrupt_reconciliation(
                    task_id,
                    baseline_runtime,
                    current_runtime,
                    f"revision-only interrupt race; current runtime returned {current_snapshot.status!r}",
                )
                raise AgentCompensationRequiredError(
                    "COMPENSATION_REQUIRED: revision changed during interrupt "
                    "and the current runtime is not confirmed terminal"
                )
            _, current_runtime = bind_codex_runtime(
                self.memory.store,
                task_id,
                current_task.revision,
                event_type=EventType.CODEX_INTERRUPTED,
                workflow_id=current_runtime.workflow_id,
                operation_id=current_runtime.operation_id,
                thread_id=current_runtime.thread_id,
                turn_id=current_runtime.turn_id,
                remote_status=current_snapshot.status or "interrupted",
                task_phase=TaskPhase.PAUSED,
                current_state=reason or "Codex turn interrupted by Supervisor.",
                event_payload={"reason": reason, "revision_changed_during_interrupt": True},
            )
            return {
                "task": self.memory.get_task(task_id).model_dump(mode="json"),
                "runtime": current_runtime.model_dump(mode="json"),
                "snapshot": current_snapshot.model_dump(mode="json"),
                "stale_runtime_interrupted": True,
                "profile": self.profile,
            }

        _, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_INTERRUPTED,
            workflow_id=baseline_runtime.workflow_id,
            operation_id=baseline_runtime.operation_id,
            thread_id=baseline_runtime.thread_id,
            turn_id=baseline_runtime.turn_id,
            remote_status=snapshot.status or "interrupted",
            task_phase=TaskPhase.PAUSED,
            current_state=reason or "Codex turn interrupted by Supervisor.",
            event_payload={"reason": reason},
        )
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
        }

    async def pending_interactions(self, task_id: str) -> dict[str, Any]:
        self.memory.get_task(task_id)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            return {"task_id": task_id, "runtime": None, "interactions": []}
        agent = await self._agent()
        interactions = await agent.list_pending_interactions(self._runtime_handle(runtime))
        return {
            "task_id": task_id,
            "runtime": runtime.model_dump(mode="json"),
            "interactions": [item.model_dump(mode="json") for item in interactions],
        }

    async def answer_interaction(
        self,
        task_id: str,
        expected_revision: int,
        interaction_id: str,
        *,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        if decision not in {None, "accept", "acceptForSession", "decline", "cancel"}:
            raise AgentPlanGateError("unsupported interaction decision")
        if scope not in {"turn", "session"}:
            raise AgentPlanGateError("scope must be turn or session")
        if decision is None and answers is None:
            raise AgentPlanGateError("decision or answers is required")
        baseline_task = self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not any(
            [runtime.workflow_id, runtime.operation_id, runtime.thread_id, runtime.turn_id]
        ):
            raise AgentPlanGateError("Task has no Codex runtime identity")
        self.coordinator.assert_safety_clear(task_id)
        agent = await self._agent()
        handle = self._runtime_handle(runtime)
        interactions = await agent.list_pending_interactions(handle)
        interaction = next(
            (item for item in interactions if item.interaction_id == str(interaction_id)),
            None,
        )
        if interaction is None:
            raise AgentPlanGateError("Unknown pending Codex interaction")

        kind = (interaction.kind or interaction.type or "").strip().lower()
        lease: WriterLeaseToken | None = None
        if kind in _MUTATION_INTERACTION_KINDS:
            execution = get_execution_state(self.memory.store, task_id)
            if execution.active_writer != ActiveWriter.CODEX:
                raise AgentPlanGateError(
                    f"{kind} requires an active CODEX writer lease; "
                    "writer ownership is not CODEX"
                )
            lease = WriterLeaseToken(
                task_id=task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=execution.writer_epoch,
                task_revision=expected_revision,
            )
        elif kind == "user_input":
            lease = None
        elif kind == "provider_request":
            raise AgentPlanGateError(
                "provider_request interactions are denied by default"
            )
        else:
            raise AgentPlanGateError(
                f"unsupported interaction kind {kind!r}"
            )

        approved = self.memory.approved_plan(task_id)
        approved_plan_id = (
            approved.plan_id
            if approved is not None and approved.status.value == "APPROVED"
            else None
        )
        approved_content = (
            approved.content
            if approved is not None and approved.status.value == "APPROVED"
            else None
        )
        snapshot = await agent.respond_interaction(
            handle,
            interaction,
            {
                "decision": decision,
                "answers": answers,
                "scope": scope,
            },
        )
        reasons = self.coordinator.post_call_stale_reasons(
            task_id,
            expected_revision,
            baseline_task,
            runtime,
            handle,
            operation="respond",
            lease=lease,
            approved_plan_id=approved_plan_id,
            approved_content=approved_content,
            remote_result=snapshot,
        )
        if reasons:
            await self.coordinator.compensate_remote(
                task_id,
                "respond",
                handle,
                "; ".join(reasons),
            )
        _, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_PROGRESS,
            workflow_id=runtime.workflow_id,
            operation_id=runtime.operation_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            remote_status=snapshot.status or runtime.remote_status,
            event_payload={"interaction_id": interaction_id, "answered": True},
        )
        return {
            "task": self.memory.get_task(task_id).model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "snapshot": snapshot.model_dump(mode="json"),
        }

    async def _latch_interrupt_reconciliation(
        self,
        task_id: str,
        baseline_runtime: CodexRuntimeState,
        current_runtime: CodexRuntimeState | None,
        reason: str,
    ) -> None:
        from codex_supervisor_bridge.memory.errors import ConflictError

        try:
            record_agent_compensation_required(
                self.memory.store,
                task_id,
                operation="interrupt",
                summary=(
                    "A stale interrupt left the current Codex runtime unconfirmed; "
                    "reconciliation is required before new Codex work."
                ),
                details={
                    "reason": reason,
                    "interrupted_workflow_id": baseline_runtime.workflow_id,
                    "interrupted_operation_id": baseline_runtime.operation_id,
                    "interrupted_thread_id": baseline_runtime.thread_id,
                    "interrupted_turn_id": baseline_runtime.turn_id,
                },
                workflow_id=current_runtime.workflow_id if current_runtime else baseline_runtime.workflow_id,
                operation_id=current_runtime.operation_id if current_runtime else baseline_runtime.operation_id,
                thread_id=current_runtime.thread_id if current_runtime else baseline_runtime.thread_id,
                turn_id=current_runtime.turn_id if current_runtime else baseline_runtime.turn_id,
            )
        except ConflictError:
            pass
