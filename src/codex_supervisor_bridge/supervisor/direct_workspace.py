from __future__ import annotations

import hashlib
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path

from codex_supervisor_bridge.backends.models import (
    ChangeReview,
    CommandResult,
    GitState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.backends.workspace import WorkspaceBackend
from codex_supervisor_bridge.bootstrap.command_auth import (
    CommandAuthorization,
    CommandAuthorizationPolicy,
    CommandRequest,
    CommandVerdict,
    authorize_command,
)
from codex_supervisor_bridge.memory.agent_safety import assert_agent_safety_clear
from codex_supervisor_bridge.memory.execution import get_execution_state
from codex_supervisor_bridge.memory.models import ActiveWriter, EvidenceType
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import (
    bind_workspace,
    complete_direct_operation,
    get_prepared_direct_operation,
    get_workspace_binding,
    mark_direct_operation_reconciliation_required,
    prepare_direct_operation,
    update_prepared_operation_details,
    update_workspace_derived,
)
from codex_supervisor_bridge.memory.workspace_models import WorkspaceBindingStatus

from .direct_models import (
    DirectWorkspaceCommandResult,
    DirectWorkspaceOpenResult,
    DirectWorkspacePatchResult,
    DirectWorkspaceReadResult,
    DirectWorkspaceStatus,
)

WorkspaceAdapterFactory = Callable[[], AbstractAsyncContextManager[WorkspaceBackend]]


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class DirectWorkspaceCoordinator:
    """Supervisor facade for ChatGPT direct local development.

    All mutations use a two-phase protocol:

    1. persist PREPARED with task revision + writer epoch;
    2. perform the external workspace mutation;
    3. read actual Git/review state;
    4. finalize only if revision and writer epoch are still unchanged.

    If step 2 may have happened but step 4 cannot safely prove continuity, the
    workspace is marked RECONCILIATION_REQUIRED rather than claiming success.
    """

    def __init__(
        self,
        memory: MemoryService,
        adapter_factory: WorkspaceAdapterFactory,
        *,
        backend_name: str = "devspace",
    ) -> None:
        self.memory = memory
        self.adapter_factory = adapter_factory
        self.backend_name = backend_name

    def status(self, task_id: str) -> DirectWorkspaceStatus:
        return DirectWorkspaceStatus(
            task=self.memory.get_task(task_id),
            execution=get_execution_state(self.memory.store, task_id),
            workspace=get_workspace_binding(self.memory.store, task_id),
            prepared_operation=get_prepared_direct_operation(self.memory.store, task_id),
        )

    async def open(
        self,
        task_id: str,
        expected_revision: int,
        *,
        repository: str | None = None,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> DirectWorkspaceOpenResult:
        task = self.memory.assert_revision(task_id, expected_revision)
        assert_agent_safety_clear(self.memory.store, task_id)
        selected_repository = (repository or task.repository or "").strip()
        if not selected_repository:
            raise ValueError("Task has no repository/project path to open")

        existing = get_workspace_binding(self.memory.store, task_id)
        if existing is not None and existing.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED:
            raise ValueError(
                "Workspace requires reconciliation before opening or replacing the supervised workspace"
            )
        if (
            existing is not None
            and existing.state == WorkspaceBindingStatus.ACTIVE
            and existing.repository == selected_repository
            and existing.workspace_mode == ("worktree" if worktree else "checkout")
            and (base_ref is None or existing.base_ref == base_ref)
        ):
            return DirectWorkspaceOpenResult(task=task, workspace=existing)

        async with self.adapter_factory() as adapter:
            remote = await adapter.open_workspace(
                selected_repository,
                worktree=worktree,
                base_ref=base_ref,
            )
            try:
                task, binding = bind_workspace(
                    self.memory.store,
                    task_id,
                    expected_revision,
                    backend_name=self.backend_name,
                    workspace_id=remote.workspace_id,
                    repository=selected_repository,
                    root=remote.root,
                    workspace_mode="worktree" if remote.worktree else "checkout",
                    base_ref=base_ref,
                    git_branch=remote.git.branch,
                    git_head=remote.git.head,
                )
            except Exception:
                try:
                    await adapter.close_workspace(remote.workspace_id)
                finally:
                    raise
        return DirectWorkspaceOpenResult(task=task, workspace=binding)

    async def read(
        self,
        task_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> DirectWorkspaceReadResult:
        task = self.memory.get_task(task_id)
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is None or binding.state == WorkspaceBindingStatus.CLOSED:
            raise ValueError("No active supervised workspace is bound to this task")
        async with self.adapter_factory() as adapter:
            content = await adapter.read(
                binding.workspace_id,
                path,
                start_line=start_line,
                end_line=end_line,
            )
        return DirectWorkspaceReadResult(
            task_revision=task.revision,
            workspace_id=binding.workspace_id,
            path=path,
            content=content,
        )

    async def refresh_git_state(self, task_id: str) -> GitState:
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is None or binding.state == WorkspaceBindingStatus.CLOSED:
            raise ValueError("No active supervised workspace is bound to this task")
        async with self.adapter_factory() as adapter:
            git = await adapter.git_state(binding.workspace_id)
        update_workspace_derived(
            self.memory.store,
            task_id,
            git_branch=git.branch,
            git_head=git.head,
            dirty=git.dirty,
            changed_files=git.changed_files,
        )
        return git

    async def show_changes(self, task_id: str) -> ChangeReview:
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is None or binding.state == WorkspaceBindingStatus.CLOSED:
            raise ValueError("No active supervised workspace is bound to this task")
        async with self.adapter_factory() as adapter:
            review = await adapter.show_changes(binding.workspace_id)
        update_workspace_derived(
            self.memory.store,
            task_id,
            review_ref=review.review_ref,
        )
        return review

    async def apply_patch(
        self,
        task_id: str,
        expected_revision: int,
        expected_writer_epoch: int,
        patch: str,
    ) -> DirectWorkspacePatchResult:
        if not patch.strip():
            raise ValueError("patch must not be empty")
        prepared = prepare_direct_operation(
            self.memory.store,
            task_id,
            expected_revision,
            expected_writer_epoch,
            operation_type="APPLY_PATCH",
            request_digest=_digest(patch),
            details={"patch_chars": len(patch)},
        )
        lease = WriterLeaseToken(
            task_id=task_id,
            writer=ActiveWriter.CHATGPT,
            writer_epoch=expected_writer_epoch,
            task_revision=prepared.task.revision,
        )

        review = ChangeReview(summary="Patch outcome unavailable")
        git = GitState(
            branch=prepared.workspace.git_branch,
            head=prepared.workspace.git_head,
            dirty=prepared.workspace.dirty,
            changed_files=prepared.workspace.changed_files,
        )
        try:
            async with self.adapter_factory() as adapter:
                patch_review = await adapter.apply_patch(
                    prepared.workspace.workspace_id,
                    patch,
                    lease=lease,
                )
                git = await adapter.git_state(prepared.workspace.workspace_id)
                aggregate_review = await adapter.show_changes(prepared.workspace.workspace_id)
                review = ChangeReview(
                    review_ref=aggregate_review.review_ref,
                    summary=aggregate_review.summary or patch_review.summary,
                    files=patch_review.files or git.changed_files,
                    patch_excerpt=None,
                )
        except Exception as exc:
            mark_direct_operation_reconciliation_required(
                self.memory.store,
                task_id,
                prepared.operation.operation_id,
                summary="Direct patch outcome could not be confirmed after the external request.",
                details={"error_type": type(exc).__name__},
            )
            raise

        completed = complete_direct_operation(
            self.memory.store,
            task_id,
            prepared.operation.operation_id,
            writer_epoch=expected_writer_epoch,
            summary=review.summary,
            git_branch=git.branch,
            git_head=git.head,
            dirty=git.dirty,
            changed_files=git.changed_files,
            change_ref=review.review_ref,
            details={
                "review_files": review.files[:50],
                "git_changed_files": git.changed_files[:50],
            },
        )
        execution = get_execution_state(self.memory.store, task_id)

        evidence_summary = (
            "Direct workspace patch requires reconciliation before further writes."
            if completed.reconciliation_required
            else review.summary
        )
        self.memory.store.add_evidence(
            task_id,
            EvidenceType.GIT_DIFF,
            "direct-workspace",
            evidence_summary,
            external_id=review.review_ref,
            metadata={
                "operation_id": completed.operation.operation_id,
                "git_head": git.head,
                "files": git.changed_files[:50],
                "reconciliation_required": completed.reconciliation_required,
            },
            created_revision=completed.task.revision,
        )

        return DirectWorkspacePatchResult(
            task=completed.task,
            execution=execution,
            workspace=completed.workspace,
            operation=completed.operation,
            review=review,
            git=git,
            reconciliation_required=completed.reconciliation_required,
        )

    async def run_command(
        self,
        task_id: str,
        expected_revision: int,
        expected_writer_epoch: int,
        command: str,
        *,
        approved: bool = False,
        policy: CommandAuthorizationPolicy = CommandAuthorizationPolicy.ASK,
    ) -> DirectWorkspaceCommandResult:
        task = self.memory.assert_revision(task_id, expected_revision)
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is None or binding.state != WorkspaceBindingStatus.ACTIVE or not binding.root:
            raise ValueError("No active supervised workspace with a local root is bound to this task")
        authorization = authorize_command(
            CommandRequest(
                task_id=task_id,
                command=command,
                cwd=Path(binding.root),
                workspace_root=Path(binding.root),
                expected_revision=expected_revision,
                current_revision=task.revision,
                writer=ActiveWriter.CHATGPT,
                writer_epoch=expected_writer_epoch,
                approved=approved,
                policy=policy,
            )
        )
        if authorization.verdict != CommandVerdict.ALLOW:
            return DirectWorkspaceCommandResult(task=task, authorization=authorization)
        prepared = prepare_direct_operation(
            self.memory.store,
            task_id,
            expected_revision,
            expected_writer_epoch,
            operation_type="RUN_COMMAND",
            request_digest=_digest(command),
            details={
                "command_chars": len(command),
                "authorization": authorization.reason,
            },
        )
        lease = WriterLeaseToken(
            task_id=task_id,
            writer=ActiveWriter.CHATGPT,
            writer_epoch=expected_writer_epoch,
            task_revision=prepared.task.revision,
        )
        try:
            async with self.adapter_factory() as adapter:
                result = await adapter.run_command(
                    prepared.workspace.workspace_id,
                    command,
                    lease=lease,
                )
        except Exception as exc:
            mark_direct_operation_reconciliation_required(
                self.memory.store,
                task_id,
                prepared.operation.operation_id,
                summary="Command outcome could not be confirmed after the external request.",
                details={"error_type": type(exc).__name__},
            )
            raise
        operation = update_prepared_operation_details(
            self.memory.store,
            task_id,
            prepared.operation.operation_id,
            details={
                "command_id": result.command_id,
                "command_status": result.status,
                "exit_code": result.exit_code,
            },
        )
        if result.status == "running":
            return DirectWorkspaceCommandResult(
                task=self.memory.get_task(task_id),
                authorization=authorization,
                command=result,
                operation=operation,
            )
        return await self._finish_command(
            task_id,
            operation.operation_id,
            authorization,
            result,
            expected_writer_epoch,
        )

    async def poll_command(
        self,
        task_id: str,
        command_id: str,
        *,
        input_text: str | None = None,
        interrupt: bool = False,
    ) -> DirectWorkspaceCommandResult:
        self.memory.get_task(task_id)
        binding = get_workspace_binding(self.memory.store, task_id)
        operation = get_prepared_direct_operation(self.memory.store, task_id)
        if binding is None or binding.state != WorkspaceBindingStatus.ACTIVE or operation is None:
            raise ValueError("No active direct command session is available")
        if operation.operation_type != "RUN_COMMAND" or operation.details.get("command_id") != command_id:
            raise ValueError("Unknown direct command session")
        async with self.adapter_factory() as adapter:
            result = await adapter.poll_command(
                binding.workspace_id,
                command_id,
                input_text=input_text,
                interrupt=interrupt,
            )
        authorization = CommandAuthorization(
            verdict=CommandVerdict.ALLOW,
            user_message="Command session is authorized for the supervised project.",
            reason="existing_authorized_session",
        )
        if result.status == "running":
            operation = update_prepared_operation_details(
                self.memory.store,
                task_id,
                operation.operation_id,
                details={"command_status": result.status},
            )
            return DirectWorkspaceCommandResult(
                task=self.memory.get_task(task_id),
                authorization=authorization,
                command=result,
                operation=operation,
            )
        return await self._finish_command(
            task_id,
            operation.operation_id,
            authorization,
            result,
            operation.writer_epoch,
        )

    async def _finish_command(
        self,
        task_id: str,
        operation_id: str,
        authorization: CommandAuthorization,
        result: CommandResult,
        writer_epoch: int,
    ) -> DirectWorkspaceCommandResult:
        binding = get_workspace_binding(self.memory.store, task_id)
        if binding is None:
            raise ValueError("No active supervised workspace is bound to this task")
        async with self.adapter_factory() as adapter:
            git = await adapter.git_state(binding.workspace_id)
            review = await adapter.show_changes(binding.workspace_id)
        completed = complete_direct_operation(
            self.memory.store,
            task_id,
            operation_id,
            writer_epoch=writer_epoch,
            summary="Direct command completed.",
            git_branch=git.branch,
            git_head=git.head,
            dirty=git.dirty,
            changed_files=git.changed_files,
            change_ref=review.review_ref,
            details={
                "command_status": result.status,
                "exit_code": result.exit_code,
                "stdout_chars": len(result.stdout),
                "stderr_chars": len(result.stderr),
            },
        )
        self.memory.store.add_evidence(
            task_id,
            EvidenceType.TEST_LOG,
            "direct-command",
            "Direct command completed and workspace state was observed.",
            external_id=result.command_id,
            metadata={
                "operation_id": operation_id,
                "status": result.status,
                "exit_code": result.exit_code,
                "truncated": result.truncated,
            },
            created_revision=completed.task.revision,
        )
        return DirectWorkspaceCommandResult(
            task=completed.task,
            authorization=authorization,
            command=result,
            operation=completed.operation,
            git=git,
            review=review,
            reconciliation_required=completed.reconciliation_required,
        )
