from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp import Client

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    GitState,
    PendingInteraction,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import get_agent_safety
from codex_supervisor_bridge.memory.backend_binding import get_task_backend_binding
from codex_supervisor_bridge.memory.codex_runtime import get_codex_runtime
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
)
from codex_supervisor_bridge.memory.models import ActiveWriter
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import get_workspace_binding
from codex_supervisor_bridge.supervisor.agent_execution import AgentExecutionCoordinator
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator
from codex_supervisor_bridge.supervisor.runtime import RuntimeComposition


class FakeWorkspace:
    async def __aenter__(self) -> "FakeWorkspace":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake-workspace",
            status=BackendHealthStatus.READY,
            user_message="Local workspace is ready.",
        )

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        return WorkspaceState(
            workspace_id="ws-e2e",
            repository=repository,
            root="C:/repo",
            worktree=worktree,
            git=GitState(branch="main", head="a" * 40),
        )

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return f"{workspace_id}:{path}:{start_line}:{end_line}"

    async def apply_patch(
        self,
        workspace_id: str,
        patch: str,
        *,
        lease: WriterLeaseToken,
    ) -> ChangeReview:
        return ChangeReview(review_ref="review-e2e", summary="patched", files=["src/app.py"])

    async def run_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("command surface is not used by this E2E")

    async def poll_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("command surface is not used by this E2E")

    async def show_changes(self, workspace_id: str) -> ChangeReview:
        return ChangeReview(review_ref="review-e2e", summary="1 file changed", files=["src/app.py"])

    async def git_state(self, workspace_id: str) -> GitState:
        return GitState(branch="main", head="a" * 40, dirty=True, changed_files=["src/app.py"])

    async def close_workspace(self, workspace_id: str) -> None:
        return None


class FakeProfileAgent:
    def __init__(self, profile: str) -> None:
        self.profile = profile
        self.calls: list[str] = []
        self.pending: list[PendingInteraction] = [
            PendingInteraction(
                interaction_id="17",
                kind="command_approval",
                summary="Run tests?",
                options=["accept", "decline"],
                runtime_reference={"request_id": 17, "method": "item/commandExecution/requestApproval"},
            )
        ]

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability=f"agent-{self.profile}",
            status=BackendHealthStatus.READY,
            user_message="Codex is ready.",
        )

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
        assert lease.writer == ActiveWriter.CODEX
        self.calls.append("start_execution")
        return PlanHandle(
            operation_id=f"{self.profile}-operation",
            workflow_id=f"{self.profile}-workflow",
            thread_id=f"{self.profile}-thread",
            turn_id=f"{self.profile}-exec-turn",
            status="executing",
        )

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        del cursor, wait_ms
        if handle.turn_id == f"{self.profile}-plan-turn" and not handle.operation_id:
            return await self.get_plan_status(handle)
        return AgentSnapshot(
            status="inProgress",
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            completed=["Codex implementation completed"],
            files_changed=["src/app.py"],
            validation={"status": "passed"},
            evidence_refs=["review-e2e"],
            pending_interactions=list(self.pending),
        )

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        assert lease.writer == ActiveWriter.CODEX
        self.calls.append("steer")
        return await self.observe(handle)

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        self.calls.append("interrupt")
        snapshot = await self.observe(handle)
        return snapshot.model_copy(update={"status": "interrupted"})

    async def list_pending_interactions(self, handle: PlanHandle) -> list[PendingInteraction]:
        return list(self.pending)

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot:
        self.calls.append("respond_interaction")
        self.pending.clear()
        return await self.observe(handle)

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)


def fake_composition(
    memory: MemoryService,
    profile: str,
) -> tuple[RuntimeComposition, FakeProfileAgent]:
    agent = FakeProfileAgent(profile)
    coordinator = AgentExecutionCoordinator(memory, agent)
    checkpoints = CheckpointService(memory, agent_backend=agent)
    return (
        RuntimeComposition(
            profile=profile,
            workspace_backend="devspace" if profile == "lightweight" else "kandev",
            agent_backend="local_codex_bridge" if profile == "lightweight" else "control_plane",
            workspace_factory=lambda: FakeWorkspace(),
            agent_coordinator=coordinator,
            checkpoint_service=checkpoints,
        ),
        agent,
    )


def test_profile_b_runtime_composition_uses_session_manager() -> None:
    memory = MemoryService()
    try:
        composition = RuntimeComposition.profile_b(
            memory,
            launch_command=["node", "dist/src/index.js"],
        )
        assert composition.profile == "lightweight"
        assert composition.session_manager is not None
        assert composition.agent_coordinator.agent_backend is composition.session_manager
        assert composition.checkpoint_service is not None
    finally:
        memory.close()


def test_profile_a_runtime_composition_keeps_control_plane_fallback() -> None:
    memory = MemoryService()
    try:
        composition = RuntimeComposition.profile_a(
            memory,
            adapter_factory=lambda: object(),
        )
        assert composition.profile == "existing"
        assert composition.session_manager is not None
        assert composition.agent_backend == "control_plane"
        assert composition.agent_coordinator.agent_backend is composition.session_manager
    finally:
        memory.close()


def test_production_composition_e2e_shared_semantics(tmp_path: Path) -> None:
    def run_profile(profile: str) -> dict[str, object]:
        database = tmp_path / f"{profile}.db"
        memory = MemoryService(database)
        composition, agent = fake_composition(memory, profile)
        direct = DirectWorkspaceCoordinator(
            memory,
            composition.workspace_factory,
            backend_name=composition.workspace_backend,
        )
        facade = composition.agent_facade(memory)
        trace: list[tuple[str, int, str, str, int, str, int]] = []

        def snapshot(stage: str) -> None:
            task = memory.get_task("TASK-E2E")
            execution = get_execution_state(memory.store, "TASK-E2E")
            binding = get_workspace_binding(memory.store, "TASK-E2E")
            trace.append(
                (
                    stage,
                    task.revision,
                    task.phase.value,
                    execution.active_writer.value,
                    execution.writer_epoch,
                    binding.workspace_id if binding else "",
                    task.plan_version,
                )
            )

        async def scenario() -> dict[str, object]:
            task = memory.create_task("TASK-E2E", "Production E2E", repository="C:/repo")
            opened = await direct.open(task.task_id, task.revision, repository="C:/repo")
            snapshot("bound")
            acquired = acquire_writer(
                memory.store,
                task.task_id,
                opened.task.revision,
                ActiveWriter.CHATGPT,
            )
            snapshot("chatgpt_writer")
            read = await direct.read(task.task_id, "src/app.py")
            assert read.workspace_id == "ws-e2e"
            patched = await direct.apply_patch(
                task.task_id,
                acquired.task.revision,
                acquired.execution.writer_epoch,
                "*** Update File: src/app.py\n@@\n-print('old')\n+print('new')\n",
            )
            snapshot("direct_patch")
            review = await direct.show_changes(task.task_id)
            assert review.review_ref == "review-e2e"
            handed = handoff_writer(
                memory.store,
                task.task_id,
                patched.task.revision,
                from_writer=ActiveWriter.CHATGPT,
                to_writer=ActiveWriter.CODEX,
                expected_writer_epoch=patched.execution.writer_epoch,
                reason="User delegated implementation",
                change_ref=review.review_ref,
                explicit_user_authorization=True,
            )
            snapshot("handoff_codex")
            await facade.start_plan(
                task.task_id,
                handed.task.revision,
                project_id="C:/repo",
            )
            snapshot("plan_started")
            status = await facade.status(task.task_id)
            assert status["snapshot"]["plan"] is not None
            imported = await facade.import_latest_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
            )
            snapshot("plan_imported")
            memory.approve_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
                imported["plan"]["plan_id"],
            )
            snapshot("plan_approved")
            executed = await facade.execute_approved_plan(
                task.task_id,
                memory.get_task(task.task_id).revision,
            )
            snapshot("execution_started")
            assert executed["plan_handle"]["turn_id"] == f"{profile}-exec-turn"
            observed = await facade.status(task.task_id)
            assert observed["snapshot"]["status"] == "inProgress"
            steered = await facade.soft_steer(
                task.task_id,
                memory.get_task(task.task_id).revision,
                "Also run the focused test.",
            )
            assert steered["snapshot"]["status"] == "inProgress"
            pending = await facade.pending_interactions(task.task_id)
            assert pending["interactions"]
            answered = await facade.answer_interaction(
                task.task_id,
                memory.get_task(task.task_id).revision,
                pending["interactions"][0]["interaction_id"],
                decision="accept",
                scope="turn",
            )
            assert answered["snapshot"]["status"] == "inProgress"
            interrupted = await facade.interrupt(
                task.task_id,
                memory.get_task(task.task_id).revision,
                reason="Scope check",
            )
            assert interrupted["snapshot"]["status"] == "interrupted"
            snapshot("interrupted")
            current = memory.get_task(task.task_id)
            execution = get_execution_state(memory.store, task.task_id)
            back = handoff_writer(
                memory.store,
                task.task_id,
                current.revision,
                from_writer=ActiveWriter.CODEX,
                to_writer=ActiveWriter.CHATGPT,
                expected_writer_epoch=execution.writer_epoch,
                reason="Codex handed the task back",
                change_ref="review-e2e",
            )
            snapshot("handback_chatgpt")
            await direct.apply_patch(
                task.task_id,
                back.task.revision,
                back.execution.writer_epoch,
                "*** Update File: src/app.py\n@@\n-print('new')\n+print('final')\n",
            )
            snapshot("direct_final_patch")
            assert "start_plan" in agent.calls
            assert "start_execution" in agent.calls
            assert "steer" in agent.calls
            assert "respond_interaction" in agent.calls
            assert "interrupt" in agent.calls

            return {
                "trace": trace,
                "events": [event.event_type.value for event in memory.timeline(task.task_id)],
                "approved_plan": memory.approved_plan(task.task_id).plan_id,  # type: ignore[union-attr]
                "binding": get_task_backend_binding(memory.store, task.task_id).model_dump(mode="json"),  # type: ignore[union-attr]
                "final_revision": memory.get_task(task.task_id).revision,
            }

        try:
            result = asyncio.run(scenario())
        finally:
            memory.close()

        reopened = MemoryService(database)
        try:
            context = reopened.get_context_pack("TASK-E2E")
            assert context.task.task_id == "TASK-E2E"
            runtime = get_codex_runtime(reopened.store, "TASK-E2E")
            assert runtime is not None and runtime.turn_id == f"{profile}-exec-turn"
            binding = get_workspace_binding(reopened.store, "TASK-E2E")
            assert binding is not None and binding.workspace_id == "ws-e2e"
            safety = get_agent_safety(reopened.store, "TASK-E2E")
            assert safety is None or safety.state == "NONE"
        finally:
            reopened.close()
        return result

    profile_a = run_profile("profile-a")
    profile_b = run_profile("profile-b")
    assert profile_a["trace"] == profile_b["trace"]
    assert profile_a["events"] == profile_b["events"]
    assert profile_a["binding"]["bound_revision"] == profile_b["binding"]["bound_revision"]
    assert profile_a["binding"]["bound_epoch"] == profile_b["binding"]["bound_epoch"] == 1
    assert profile_a["binding"]["profile"] == "profile-a"
    assert profile_b["binding"]["profile"] == "profile-b"
    assert profile_a["approved_plan"] != profile_b["approved_plan"]
    assert profile_a["final_revision"] == profile_b["final_revision"]


def test_mcp_semantic_facade_exposes_neutral_tools() -> None:
    memory = MemoryService()
    try:
        composition, _ = fake_composition(memory, "lightweight")
        facade = composition.agent_facade(memory)
        from codex_supervisor_bridge.mcp.server import create_mcp_server

        server = create_mcp_server(
            memory,
            agent_facade=facade,
            checkpoints=composition.checkpoint_service,
            direct_workspace=DirectWorkspaceCoordinator(
                memory,
                lambda: FakeWorkspace(),
                backend_name="devspace",
            ),
        )

        async def scenario() -> None:
            async with Client(server) as client:
                tools = {tool.name for tool in (await client.list_tools()).tools}
                assert {
                    "get_codex_control_health",
                    "start_codex_plan",
                    "execute_codex_approved_plan",
                    "soft_steer_codex",
                    "interrupt_codex",
                    "answer_codex_pending_interaction",
                } <= tools
                capabilities = await client.call_tool("get_codex_runtime_capabilities", {})
                rendered = str(capabilities.content)
                assert "profile" in rendered

        asyncio.run(scenario())
    finally:
        memory.close()
