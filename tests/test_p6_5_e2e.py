from __future__ import annotations

import asyncio
from pathlib import Path

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    GitState,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.checkpoint_store import latest_checkpoint
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime, get_codex_runtime
from codex_supervisor_bridge.memory.execution import acquire_writer, handoff_writer
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import get_workspace_binding
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator


class FakeWorkspace:
    def __init__(self) -> None:
        self.changed = ["src/app.py"]

    async def __aenter__(self) -> "FakeWorkspace":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake",
            status=BackendHealthStatus.READY,
            user_message="ready",
        )

    async def open_workspace(self, repository: str, *, worktree: bool = True, base_ref: str | None = None) -> WorkspaceState:
        return WorkspaceState(
            workspace_id="ws-e2e",
            repository=repository,
            root="C:/repo",
            worktree=worktree,
            git=GitState(branch="main", head="a" * 40),
        )

    async def read(self, workspace_id: str, path: str, *, start_line: int | None = None, end_line: int | None = None) -> str:
        return f"{workspace_id}:{path}:{start_line}:{end_line}"

    async def apply_patch(self, workspace_id: str, patch: str, *, lease: WriterLeaseToken) -> ChangeReview:
        return ChangeReview(review_ref="review-e2e", summary="patched", files=self.changed)

    async def run_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("command surface is intentionally not used by this test")

    async def poll_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("command surface is intentionally not used by this test")

    async def show_changes(self, workspace_id: str) -> ChangeReview:
        return ChangeReview(review_ref="review-e2e", summary="1 file changed", files=self.changed)

    async def git_state(self, workspace_id: str) -> GitState:
        return GitState(branch="main", head="a" * 40, dirty=True, changed_files=self.changed)

    async def close_workspace(self, workspace_id: str) -> None:
        return None


class FakeAgent:
    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake-agent",
            status=BackendHealthStatus.READY,
            user_message="ready",
        )

    async def start_plan(self, *, task_id: str, context_pack: str, workspace: WorkspaceState) -> PlanHandle:
        return PlanHandle(workflow_id="wf-e2e", thread_id="thread-e2e", turn_id="turn-plan", status="planning")

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)

    async def start_execution(self, *, task_id: str, context_pack: str, approved_plan: str, workspace: WorkspaceState, lease: WriterLeaseToken) -> PlanHandle:
        assert lease.writer == ActiveWriter.CODEX
        return PlanHandle(operation_id="op-e2e", workflow_id="wf-e2e", thread_id="thread-e2e", turn_id="turn-exec", status="executing")

    async def observe(self, handle: PlanHandle, *, cursor: int | None = None, wait_ms: int = 0) -> AgentSnapshot:
        return AgentSnapshot(
            status="completed",
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            completed=["Codex implementation completed"],
            files_changed=["src/app.py"],
            validation={"status": "passed", "pytest": "passed"},
            evidence_refs=["review-e2e"],
        )

    async def steer(self, handle: PlanHandle, instruction: str, *, lease: WriterLeaseToken) -> AgentSnapshot:
        assert lease.writer == ActiveWriter.CODEX
        return await self.observe(handle)

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        snapshot = await self.observe(handle)
        return snapshot.model_copy(update={"status": "interrupted"})

    async def list_pending_interactions(self, handle: PlanHandle) -> list[object]:
        return []

    async def respond_interaction(self, handle: PlanHandle, interaction: object, response: dict[str, object]) -> AgentSnapshot:
        return await self.observe(handle)

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)


def test_direct_codex_direct_round_trip_survives_restart(tmp_path: Path) -> None:
    database = tmp_path / "p65-e2e.db"
    memory = MemoryService(database)
    workspace = FakeWorkspace()
    agent = FakeAgent()

    async def scenario() -> None:
        task = memory.store.create_task("P65-E2E", "Round trip", repository="C:/repo")
        coordinator = DirectWorkspaceCoordinator(memory, lambda: workspace, backend_name="fake-devspace")
        opened = await coordinator.open(task.task_id, task.revision, repository="C:/repo")
        acquired = acquire_writer(memory.store, task.task_id, opened.task.revision, ActiveWriter.CHATGPT)
        patched = await coordinator.apply_patch(
            task.task_id,
            acquired.task.revision,
            acquired.execution.writer_epoch,
            "*** Update File: src/app.py\n@@\n-print('old')\n+print('new')\n",
        )
        handed = handoff_writer(
            memory.store,
            task.task_id,
            patched.task.revision,
            from_writer=ActiveWriter.CHATGPT,
            to_writer=ActiveWriter.CODEX,
            expected_writer_epoch=patched.execution.writer_epoch,
            reason="User delegated the bounded implementation",
            change_ref=patched.review.review_ref,
            explicit_user_authorization=True,
        )

        plan = memory.create_plan(task.task_id, handed.task.revision, "1. Implement\n2. Test")
        plan_revision = memory.get_task(task.task_id).revision
        approved = memory.approve_plan(task.task_id, plan_revision, plan.plan_id)
        await agent.start_plan(
            task_id=task.task_id,
            context_pack=memory.get_context_pack(task.task_id).content,
            workspace=WorkspaceState(workspace_id="ws-e2e", repository="C:/repo", root="C:/repo"),
        )
        execution_handle = await agent.start_execution(
            task_id=task.task_id,
            context_pack=memory.get_context_pack(task.task_id).content,
            approved_plan=approved.content,
            workspace=WorkspaceState(workspace_id="ws-e2e", repository="C:/repo", root="C:/repo"),
            lease=WriterLeaseToken(
                task_id=task.task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=handed.execution.writer_epoch,
                task_revision=memory.get_task(task.task_id).revision,
            ),
        )
        task_after_runtime, _ = bind_codex_runtime(
            memory.store,
            task.task_id,
            memory.get_task(task.task_id).revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id=execution_handle.workflow_id,
            operation_id=execution_handle.operation_id,
            thread_id=execution_handle.thread_id,
            turn_id=execution_handle.turn_id,
            remote_status="executing",
            task_phase=TaskPhase.IMPLEMENTING,
        )
        observed = await agent.observe(execution_handle)
        assert observed.status == "completed"
        steered = await agent.steer(
            execution_handle,
            "Also run the focused test.",
            lease=WriterLeaseToken(
                task_id=task.task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=handed.execution.writer_epoch,
                task_revision=task_after_runtime.revision,
            ),
        )
        assert steered.validation["status"] == "passed"
        interrupted = await agent.interrupt(execution_handle)
        assert interrupted.status == "interrupted"
        checkpoint = await CheckpointService(memory, agent_backend=agent).collect(task.task_id)
        assert checkpoint.checkpoint.files_changed == ["src/app.py"]

        current = memory.get_task(task.task_id)
        back = handoff_writer(
            memory.store,
            task.task_id,
            current.revision,
            from_writer=ActiveWriter.CODEX,
            to_writer=ActiveWriter.CHATGPT,
            expected_writer_epoch=handed.execution.writer_epoch,
            reason="Codex handed the task back for final direct edits",
            change_ref="review-e2e",
        )
        final = await coordinator.apply_patch(
            task.task_id,
            back.task.revision,
            back.execution.writer_epoch,
            "*** Update File: src/app.py\n@@\n-print('new')\n+print('final')\n",
        )
        assert final.task.task_id == task.task_id
        assert final.task.revision > current.revision

    try:
        asyncio.run(scenario())
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        assert reopened.get_task("P65-E2E").task_id == "P65-E2E"
        assert get_workspace_binding(reopened.store, "P65-E2E").workspace_id == "ws-e2e"  # type: ignore[union-attr]
        assert get_codex_runtime(reopened.store, "P65-E2E").thread_id == "thread-e2e"  # type: ignore[union-attr]
        assert latest_checkpoint(reopened.store, "P65-E2E") is not None
        assert reopened.get_context_pack("P65-E2E").task.task_id == "P65-E2E"
    finally:
        reopened.close()
