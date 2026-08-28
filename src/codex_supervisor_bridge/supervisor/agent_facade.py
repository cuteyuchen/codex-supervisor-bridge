from __future__ import annotations

from typing import Any, Protocol

from codex_supervisor_bridge.backends.models import (
    GitState,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
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

from .agent_execution import AgentExecutionCoordinator, AgentPlanGateError
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

        existing = assert_task_backend_binding(
            self.memory.store,
            task_id,
            workspace_backend=self.workspace_backend,
            agent_backend=self.agent_backend,
        )
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
            return self.session_manager.backend
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
        self.memory.assert_revision(task_id, expected_revision)
        execution = get_execution_state(self.memory.store, task_id)
        if execution.active_writer != ActiveWriter.CODEX:
            raise AgentPlanGateError("Codex steering requires an active CODEX writer lease")
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise AgentPlanGateError("Task has no active Codex runtime")
        agent = await self._agent()
        handle = self._runtime_handle(runtime)
        snapshot = await agent.steer(
            handle,
            instruction.strip(),
            lease=WriterLeaseToken(
                task_id=task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=execution.writer_epoch,
                task_revision=expected_revision,
            ),
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
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not any(
            [runtime.workflow_id, runtime.operation_id, runtime.thread_id, runtime.turn_id]
        ):
            raise AgentPlanGateError("Task has no Codex runtime to interrupt")
        agent = await self._agent()
        snapshot = await agent.interrupt(self._runtime_handle(runtime))
        _, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_INTERRUPTED,
            workflow_id=runtime.workflow_id,
            operation_id=runtime.operation_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
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
        self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise AgentPlanGateError("Task has no Codex runtime")
        agent = await self._agent()
        handle = self._runtime_handle(runtime)
        interactions = await agent.list_pending_interactions(handle)
        interaction = next(
            (item for item in interactions if item.interaction_id == str(interaction_id)),
            None,
        )
        if interaction is None:
            raise AgentPlanGateError("Unknown pending Codex interaction")
        snapshot = await agent.respond_interaction(
            handle,
            interaction,
            {
                "decision": decision,
                "answers": answers,
                "scope": scope,
            },
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
