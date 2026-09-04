from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path

import pytest

from codex_supervisor_bridge.backends.models import (
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    CommandResult,
    GitState,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.bootstrap.command_auth import CommandAuthorizationPolicy
from codex_supervisor_bridge.memory.execution import acquire_writer, get_execution_state
from codex_supervisor_bridge.memory.models import ActiveWriter, Actor
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import (
    get_active_direct_command_session,
    get_direct_command_session,
    get_workspace_binding,
)
from codex_supervisor_bridge.memory.workspace_models import (
    DirectCommandSessionStatus,
    DirectOperationStatus,
    WorkspaceBindingStatus,
)
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator


class FakeWorkspaceAdapter:
    def __init__(
        self,
        *,
        on_patch: Callable[[], None] | None = None,
        patch_error: Exception | None = None,
        poll_error: Exception | None = None,
    ) -> None:
        self.on_patch = on_patch
        self.patch_error = patch_error
        self.poll_error = poll_error
        self.open_calls = 0
        self.patch_calls = 0
        self.command_calls = 0
        self.command_result = CommandResult(
            command_id="17",
            status="completed",
            exit_code=0,
            stdout="ok",
        )
        self.poll_result = CommandResult(
            command_id="17",
            status="completed",
            exit_code=0,
            stdout="done",
        )
        self.git = GitState(
            branch="main",
            head="a" * 40,
            dirty=False,
            changed_files=[],
        )

    async def __aenter__(self) -> "FakeWorkspaceAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake",
            status=BackendHealthStatus.READY,
            user_message="ready",
        )

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        self.open_calls += 1
        return WorkspaceState(
            workspace_id="ws-direct",
            repository=repository,
            root="C:/worktrees/ws-direct",
            worktree=worktree,
            git=self.git,
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
        self.patch_calls += 1
        if self.on_patch is not None:
            self.on_patch()
        # Treat the external write as already sent before a possible exception.
        self.git = GitState(
            branch="main",
            head="b" * 40,
            dirty=True,
            changed_files=["src/app.py"],
        )
        if self.patch_error is not None:
            raise self.patch_error
        return ChangeReview(summary="Applied patch", files=["src/app.py"])

    async def run_command(
        self,
        workspace_id: str,
        command: str,
        *,
        lease: WriterLeaseToken,
        yield_time_ms: int = 10_000,
        tty: bool = False,
    ) -> CommandResult:
        self.command_calls += 1
        return self.command_result

    async def poll_command(
        self,
        workspace_id: str,
        command_id: str,
        *,
        input_text: str | None = None,
        interrupt: bool = False,
    ) -> CommandResult:
        if self.poll_error is not None:
            raise self.poll_error
        return self.poll_result

    async def show_changes(self, workspace_id: str) -> ChangeReview:
        return ChangeReview(
            review_ref="review-direct-1",
            summary="1 file changed",
            files=["src/app.py"],
        )

    async def git_state(self, workspace_id: str) -> GitState:
        return self.git

    async def close_workspace(self, workspace_id: str) -> None:
        return None


def setup_direct(memory: MemoryService, adapter: FakeWorkspaceAdapter) -> tuple[DirectWorkspaceCoordinator, int, int]:
    task = memory.create_task(
        "DIRECT-COORD",
        "Direct coordinator",
        repository="C:/src/project",
        goal="Patch the project directly.",
    )
    coordinator = DirectWorkspaceCoordinator(memory, lambda: adapter)
    opened = asyncio.run(
        coordinator.open(
            task.task_id,
            task.revision,
            worktree=True,
            base_ref="main",
        )
    )
    acquired = acquire_writer(
        memory.store,
        task.task_id,
        opened.task.revision,
        ActiveWriter.CHATGPT,
        actor=Actor.USER,
    )
    return coordinator, acquired.task.revision, acquired.execution.writer_epoch


def test_open_read_and_patch_are_durable_and_revision_fenced(tmp_path: Path) -> None:
    database = tmp_path / "direct.db"
    memory = MemoryService(database)
    adapter = FakeWorkspaceAdapter()
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        read = await coordinator.read("DIRECT-COORD", "src/app.py", start_line=2, end_line=4)
        assert read.task_revision == revision
        assert read.content == "ws-direct:src/app.py:2:4"
        assert memory.get_task("DIRECT-COORD").revision == revision

        patched = await coordinator.apply_patch(
            "DIRECT-COORD",
            revision,
            epoch,
            "*** Begin Patch\n*** End Patch",
        )
        assert patched.reconciliation_required is False
        assert patched.operation.status == DirectOperationStatus.SUCCEEDED
        assert patched.workspace.state == WorkspaceBindingStatus.ACTIVE
        assert patched.review.review_ref == "review-direct-1"
        assert patched.git.head == "b" * 40
        # PREPARED and completion are two distinct supervised mutations.
        assert patched.task.revision == revision + 2
        assert patched.execution.active_writer == ActiveWriter.CHATGPT
        assert patched.execution.writer_epoch == epoch

    try:
        asyncio.run(scenario())
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        binding = get_workspace_binding(reopened.store, "DIRECT-COORD")
        assert binding is not None
        assert binding.workspace_id == "ws-direct"
        assert binding.last_review_ref == "review-direct-1"
        assert binding.git_head == "b" * 40
        assert get_execution_state(reopened.store, "DIRECT-COORD").active_writer == ActiveWriter.CHATGPT
    finally:
        reopened.close()


def test_direct_command_defaults_to_ask_and_records_completed_evidence() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter()
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        pending = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "pytest",
        )
        assert pending.authorization.verdict.value == "ASK"
        assert pending.command is None
        assert adapter.command_calls == 0

        completed = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "pytest",
            approved=True,
            policy=CommandAuthorizationPolicy.ASK,
        )
        assert completed.authorization.verdict.value == "ALLOW"
        assert completed.command is not None
        assert completed.command.exit_code == 0
        assert completed.operation is not None
        assert completed.operation.status == DirectOperationStatus.SUCCEEDED
        assert adapter.command_calls == 1

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_direct_command_long_session_can_be_polled_and_interrupted(tmp_path: Path) -> None:
    database = tmp_path / "direct-command.db"
    memory = MemoryService(database)
    adapter = FakeWorkspaceAdapter()
    adapter.command_result = CommandResult(command_id="18", status="running")
    adapter.poll_result = CommandResult(command_id="18", status="completed", exit_code=0, stdout="done")
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def start() -> None:
        started = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "python -c pass",
            approved=True,
            policy=CommandAuthorizationPolicy.ALLOW,
        )
        assert started.command is not None and started.command.status == "running"
        assert started.operation is not None and started.operation.status == DirectOperationStatus.PREPARED
        session = get_active_direct_command_session(memory.store, "DIRECT-COORD")
        assert session is not None
        assert session.status == DirectCommandSessionStatus.RUNNING

    try:
        asyncio.run(start())
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        async def finish() -> None:
            recovered = DirectWorkspaceCoordinator(reopened, lambda: adapter)
            recovered_session = get_direct_command_session(reopened.store, "DIRECT-COORD", "18")
            assert recovered_session is not None
            assert recovered_session.status == DirectCommandSessionStatus.RUNNING
            finished = await recovered.poll_command("DIRECT-COORD", "18", interrupt=True)
            assert finished.command is not None and finished.command.status == "completed"
            assert finished.operation is not None and finished.operation.status == DirectOperationStatus.SUCCEEDED
            persisted = get_direct_command_session(reopened.store, "DIRECT-COORD", "18")
            assert persisted is not None
            assert persisted.status == DirectCommandSessionStatus.COMPLETED
            assert persisted.completed_revision == finished.task.revision

        asyncio.run(finish())
    finally:
        reopened.close()


def test_unknown_direct_command_outcome_requires_workspace_reconciliation() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter()
    adapter.command_result = CommandResult(command_id="19", status="unknown")
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        result = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "pytest",
            approved=True,
            policy=CommandAuthorizationPolicy.ALLOW,
        )
        assert result.reconciliation_required is True
        assert result.operation is not None
        assert result.operation.status == DirectOperationStatus.RECONCILIATION_REQUIRED

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_direct_command_poll_failure_latches_unknown_session() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter(poll_error=RuntimeError("transport disconnected"))
    adapter.command_result = CommandResult(command_id="21", status="running")
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        started = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "pytest",
            approved=True,
            policy=CommandAuthorizationPolicy.ALLOW,
        )
        assert started.command is not None and started.command.status == "running"
        with pytest.raises(RuntimeError, match="transport disconnected"):
            await coordinator.poll_command("DIRECT-COORD", "21")
        session = get_direct_command_session(memory.store, "DIRECT-COORD", "21")
        assert session is not None
        assert session.status == DirectCommandSessionStatus.UNKNOWN
        status = coordinator.status("DIRECT-COORD")
        assert status.workspace is not None
        assert status.workspace.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_direct_command_output_is_bounded_at_supervisor_boundary() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter()
    adapter.command_result = CommandResult(command_id="20", status="completed", exit_code=0, stdout="x" * 25_000)
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        result = await coordinator.run_command(
            "DIRECT-COORD",
            revision,
            epoch,
            "pytest",
            approved=True,
            policy=CommandAuthorizationPolicy.ALLOW,
        )
        assert result.command is not None
        assert len(result.command.stdout) == 20_000
        assert result.command.truncated is True

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_user_intent_change_while_patch_is_in_flight_requires_reconciliation() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter()
    coordinator, revision, epoch = setup_direct(memory, adapter)

    def mutate_intent() -> None:
        task = memory.get_task("DIRECT-COORD")
        memory.update_intent(
            task.task_id,
            task.revision,
            "User changed the goal while the direct patch was in flight.",
        )

    adapter.on_patch = mutate_intent

    async def scenario() -> None:
        result = await coordinator.apply_patch(
            "DIRECT-COORD",
            revision,
            epoch,
            "*** Begin Patch\n*** End Patch",
        )
        assert result.reconciliation_required is True
        assert result.operation.status == DirectOperationStatus.RECONCILIATION_REQUIRED
        assert result.workspace.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED
        assert result.task.current_goal == "User changed the goal while the direct patch was in flight."
        assert result.task.revision == revision + 2  # PREPARED + user intent mutation; no fake success bump

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_external_patch_exception_marks_unknown_workspace_for_reconciliation() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter(patch_error=RuntimeError("transport disconnected"))
    coordinator, revision, epoch = setup_direct(memory, adapter)

    async def scenario() -> None:
        with pytest.raises(RuntimeError, match="transport disconnected"):
            await coordinator.apply_patch(
                "DIRECT-COORD",
                revision,
                epoch,
                "*** Begin Patch\n*** End Patch",
            )
        status = coordinator.status("DIRECT-COORD")
        assert status.workspace is not None
        assert status.workspace.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED
        assert status.prepared_operation is None

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_open_is_idempotent_for_existing_active_binding() -> None:
    memory = MemoryService()
    adapter = FakeWorkspaceAdapter()
    task = memory.create_task("OPEN-IDEMPOTENT", "Open once", repository="C:/src/project")
    coordinator = DirectWorkspaceCoordinator(memory, lambda: adapter)

    async def scenario() -> None:
        first = await coordinator.open(task.task_id, task.revision, worktree=True, base_ref="main")
        assert adapter.open_calls == 1
        second = await coordinator.open(
            task.task_id,
            first.task.revision,
            worktree=True,
            base_ref="main",
        )
        assert second.workspace.workspace_id == first.workspace.workspace_id
        assert second.task.revision == first.task.revision
        assert adapter.open_calls == 1

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
