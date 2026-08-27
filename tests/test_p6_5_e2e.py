from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    GitState,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.checkpoint_store import latest_checkpoint
from codex_supervisor_bridge.memory.codex_runtime import get_codex_runtime
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
)
from codex_supervisor_bridge.memory.models import ActiveWriter, Actor
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import get_workspace_binding
from codex_supervisor_bridge.supervisor.agent_execution import (
    AgentExecutionCoordinator,
    AgentPlanGateError,
)
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
        snapshot = await self.observe(handle)
        return snapshot.model_copy(
            update={"status": "completed", "plan": PlanResult(content="1. Implement\n2. Test")}
        )

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


class FakeProfileAgent(FakeAgent):
    """Two protocol-compatible fake profiles sharing one Supervisor flow."""

    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.calls: list[str] = []

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        del task_id, context_pack, workspace
        self.calls.append("start_plan")
        return PlanHandle(
            workflow_id=f"{self.profile}-workflow",
            thread_id=f"{self.profile}-thread",
            turn_id=f"{self.profile}-plan-turn",
            status="planning",
        )

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        self.calls.append("get_plan_status")
        return AgentSnapshot(
            status="completed",
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            plan=PlanResult(content="1. Implement\n2. Test", status="ready"),
        )

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        del task_id, context_pack, workspace
        assert approved_plan == "1. Implement\n2. Test"
        assert lease.writer.value == "CODEX"
        self.calls.append("start_execution")
        return PlanHandle(
            operation_id=f"{self.profile}-operation",
            workflow_id=f"{self.profile}-workflow",
            thread_id=f"{self.profile}-thread",
            turn_id=f"{self.profile}-execution-turn",
            status="executing",
        )


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

        agent_coordinator = AgentExecutionCoordinator(memory, agent)
        plan_handle = await agent_coordinator.start_plan(
            task.task_id,
            handed.task.revision,
            context_pack=memory.get_context_pack(task.task_id).content,
            workspace=WorkspaceState(workspace_id="ws-e2e", repository="C:/repo", root="C:/repo"),
        )
        plan_snapshot = await agent.get_plan_status(plan_handle)
        assert plan_snapshot.plan is not None
        plan = agent_coordinator.import_plan(
            task.task_id,
            memory.get_task(task.task_id).revision,
            plan_snapshot,
        )
        approved = memory.approve_plan(
            task.task_id,
            memory.get_task(task.task_id).revision,
            plan.plan_id,
        )
        task = memory.get_task(task.task_id)
        execution_handle = await agent_coordinator.start_execution(
            task.task_id,
            task.revision,
            plan_id=approved.plan_id,
            plan_handle=plan_handle,
            plan_result=plan_snapshot.plan,
            context_pack=memory.get_context_pack(task.task_id).content,
            workspace=WorkspaceState(workspace_id="ws-e2e", repository="C:/repo", root="C:/repo"),
            lease=WriterLeaseToken(
                task_id=task.task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=handed.execution.writer_epoch,
                task_revision=task.revision,
            ),
        )
        task_after_runtime = memory.get_task(task.task_id)
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


def test_backend_profiles_share_supervisor_plan_gate_semantics() -> None:
    async def run_profile(profile: str) -> dict[str, object]:
        memory = MemoryService()
        agent = FakeProfileAgent(profile)
        coordinator = AgentExecutionCoordinator(memory, agent)
        workspace = WorkspaceState(workspace_id=f"{profile}-workspace", repository="C:/repo")
        try:
            task = memory.store.create_task(
                f"P65-{profile}",
                "Backend-neutral plan gate",
                repository="C:/repo",
            )
            plan_handle = await coordinator.start_plan(
                task.task_id,
                task.revision,
                context_pack="Goal: implement and test",
                workspace=workspace,
            )
            task = memory.get_task(task.task_id)
            plan_snapshot = await agent.get_plan_status(plan_handle)
            assert plan_snapshot.plan is not None

            no_writer_lease = WriterLeaseToken(
                task_id=task.task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=1,
                task_revision=task.revision,
            )
            with pytest.raises(AgentPlanGateError, match="active CODEX writer lease"):
                await coordinator.start_execution(
                    task.task_id,
                    task.revision,
                    plan_id="missing",
                    plan_handle=plan_handle,
                    plan_result=plan_snapshot.plan,
                    context_pack="Goal: implement and test",
                    workspace=workspace,
                    lease=no_writer_lease,
                )

            acquired = acquire_writer(
                memory.store,
                task.task_id,
                task.revision,
                ActiveWriter.CODEX,
                explicit_user_authorization=True,
            )
            task = acquired.task
            lease = WriterLeaseToken(
                task_id=task.task_id,
                writer=ActiveWriter.CODEX,
                writer_epoch=acquired.execution.writer_epoch,
                task_revision=task.revision,
            )
            with pytest.raises(AgentPlanGateError, match="No locally APPROVED"):
                await coordinator.start_execution(
                    task.task_id,
                    task.revision,
                    plan_id="missing",
                    plan_handle=plan_handle,
                    plan_result=plan_snapshot.plan,
                    context_pack="Goal: implement and test",
                    workspace=workspace,
                    lease=lease,
                )
            assert "start_execution" not in agent.calls

            draft = coordinator.import_plan(task.task_id, task.revision, plan_snapshot)
            approved = memory.approve_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
                draft.plan_id,
            )
            task = memory.get_task(task.task_id)
            execution = await coordinator.start_execution(
                task.task_id,
                task.revision,
                plan_id=approved.plan_id,
                plan_handle=plan_handle,
                plan_result=plan_snapshot.plan,
                context_pack="Goal: implement and test",
                workspace=workspace,
                lease=WriterLeaseToken(
                    task_id=task.task_id,
                    writer=ActiveWriter.CODEX,
                    writer_epoch=lease.writer_epoch,
                    task_revision=task.revision,
                ),
            )
            assert execution.status == "executing"
            assert agent.calls.count("start_execution") == 1

            replacement = memory.store.create_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
                "1. Replace the implementation\n2. Skip tests",
                actor=Actor.CODEX,
            )
            replacement_approved = memory.approve_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
                replacement.plan_id,
            )
            current = memory.get_task(task.task_id)
            with pytest.raises(AgentPlanGateError, match="stale or superseded"):
                await coordinator.start_execution(
                    task.task_id,
                    current.revision,
                    plan_id=approved.plan_id,
                    plan_handle=plan_handle,
                    plan_result=plan_snapshot.plan,
                    context_pack="Goal: implement and test",
                    workspace=workspace,
                    lease=WriterLeaseToken(
                        task_id=task.task_id,
                        writer=ActiveWriter.CODEX,
                        writer_epoch=lease.writer_epoch,
                        task_revision=current.revision,
                    ),
                )
            assert replacement_approved.status.value == "APPROVED"
            assert agent.calls.count("start_execution") == 1

            current = memory.get_task(task.task_id)
            state = get_execution_state(memory.store, task.task_id)
            return {
                "phase": current.phase.value,
                "intent_version": current.intent_version,
                "plan_version": current.plan_version,
                "revision": current.revision,
                "active_writer": state.active_writer.value,
                "writer_epoch": state.writer_epoch,
                "event_types": [event.event_type.value for event in memory.timeline(task.task_id)],
            }
        finally:
            memory.close()

    async def scenario() -> None:
        profile_a = await run_profile("profile-a")
        profile_b = await run_profile("profile-b")
        assert profile_a == profile_b

    asyncio.run(scenario())
