from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
    list_execution_handoffs,
    release_writer,
    set_execution_mode,
    set_handoff_policy,
)
from codex_supervisor_bridge.memory.execution_models import (
    ExecutionHandoff,
    ExecutionMutationResult,
    ExecutionState,
)
from codex_supervisor_bridge.memory.models import (
    ActiveWriter,
    Actor,
    ExecutionMode,
    HandoffPolicy,
)
from codex_supervisor_bridge.memory.service import MemoryService

from .errors import expose_memory_errors, tool_argument_error

READ_ONLY = ToolAnnotations(
    read_only_hint=True,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

MUTATION = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)


def register_execution_tools(server: MCPServer, service: MemoryService) -> None:
    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def get_task_execution_state(task_id: str) -> ExecutionState:
        """Read task-scoped execution mode, current writer, delegation policy and writer epoch."""
        return get_execution_state(service.store, task_id)

    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def get_execution_handoffs(task_id: str, limit: int = 20) -> list[ExecutionHandoff]:
        """Read recent direct-development/Codex handoffs without mutating task state."""
        if not 1 <= limit <= 100:
            raise tool_argument_error("limit must be between 1 and 100")
        return list_execution_handoffs(service.store, task_id, limit=limit)

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def set_task_execution_mode(
        task_id: str,
        expected_revision: int,
        mode: ExecutionMode,
    ) -> ExecutionMutationResult:
        """Apply the user's requested development style to this task.

        DIRECT means ChatGPT Web may be the writer. HYBRID allows either writer
        with explicit lease handoff. CODEX_SUPERVISED reserves mutations for
        Codex. An incompatible active writer must be released/handed off first.
        """
        return set_execution_mode(
            service.store,
            task_id,
            expected_revision,
            mode,
            actor=Actor.USER,
        )

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def set_codex_delegation_policy(
        task_id: str,
        expected_revision: int,
        allow_supervisor_delegation: bool,
    ) -> ExecutionMutationResult:
        """Record whether the user allows ChatGPT to decide when HYBRID work is delegated.

        False is MANUAL_ONLY. True is SUPERVISOR_ALLOWED. Enabling this is
        recorded as a user policy decision; it is not inferred from elapsed
        time, token count, or task size.
        """
        policy = (
            HandoffPolicy.SUPERVISOR_ALLOWED
            if allow_supervisor_delegation
            else HandoffPolicy.MANUAL_ONLY
        )
        return set_handoff_policy(
            service.store,
            task_id,
            expected_revision,
            policy,
            actor=Actor.USER,
        )

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def acquire_chatgpt_workspace_writer(
        task_id: str,
        expected_revision: int,
    ) -> ExecutionMutationResult:
        """Acquire the single-writer lease for ChatGPT direct local development.

        Use before any direct workspace mutation. It is allowed only in DIRECT
        or HYBRID mode and fails if another writer already owns the worktree.
        """
        return acquire_writer(
            service.store,
            task_id,
            expected_revision,
            ActiveWriter.CHATGPT,
            actor=Actor.SUPERVISOR,
        )

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def acquire_codex_workspace_writer(
        task_id: str,
        expected_revision: int,
        user_explicitly_authorized: bool = False,
    ) -> ExecutionMutationResult:
        """Acquire the single-writer lease for Codex implementation.

        In HYBRID + MANUAL_ONLY, user_explicitly_authorized may be true only
        when the current effective user instruction explicitly asks to hand
        work to Codex. SUPERVISOR_ALLOWED needs no per-handoff user prompt.
        Plan Gate rules still apply separately before Codex writes code.
        """
        return acquire_writer(
            service.store,
            task_id,
            expected_revision,
            ActiveWriter.CODEX,
            actor=Actor.SUPERVISOR,
            explicit_user_authorization=user_explicitly_authorized,
        )

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def release_workspace_writer(
        task_id: str,
        expected_revision: int,
        writer: ActiveWriter,
        expected_writer_epoch: int,
    ) -> ExecutionMutationResult:
        """Release the current workspace writer using task revision + writer-epoch fencing."""
        if writer == ActiveWriter.NONE:
            raise tool_argument_error("writer must be CHATGPT or CODEX")
        return release_writer(
            service.store,
            task_id,
            expected_revision,
            writer,
            expected_writer_epoch,
            actor=Actor.SUPERVISOR,
        )

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def handoff_workspace_writer(
        task_id: str,
        expected_revision: int,
        from_writer: ActiveWriter,
        to_writer: ActiveWriter,
        expected_writer_epoch: int,
        reason: str,
        git_head: str | None = None,
        change_ref: str | None = None,
        validation: dict[str, object] | None = None,
        user_explicitly_authorized_codex: bool = False,
    ) -> ExecutionMutationResult:
        """Atomically transfer write ownership between ChatGPT and Codex.

        Include the latest Git/review/validation handoff evidence when known.
        This operation does not itself start Codex or mutate files. In HYBRID
        MANUAL_ONLY mode, handing to Codex requires a current explicit user
        authorization; otherwise set delegation policy first only when the user
        actually granted ongoing Supervisor delegation permission.
        """
        return handoff_writer(
            service.store,
            task_id,
            expected_revision,
            from_writer=from_writer,
            to_writer=to_writer,
            expected_writer_epoch=expected_writer_epoch,
            reason=reason,
            git_head=git_head,
            change_ref=change_ref,
            validation=validation,
            actor=Actor.SUPERVISOR,
            explicit_user_authorization=user_explicitly_authorized_codex,
        )
