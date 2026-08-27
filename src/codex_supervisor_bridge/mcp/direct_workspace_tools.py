from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.backends.models import GitState
from codex_supervisor_bridge.supervisor.direct_models import (
    DirectWorkspaceOpenResult,
    DirectWorkspacePatchResult,
    DirectWorkspaceReadResult,
    DirectWorkspaceStatus,
)
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator

from .errors import expose_integration_errors, expose_memory_errors, tool_argument_error

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


def register_direct_workspace_tools(
    server: MCPServer,
    coordinator: DirectWorkspaceCoordinator,
) -> None:
    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def get_direct_workspace_status(task_id: str) -> DirectWorkspaceStatus:
        """Read the supervised workspace binding, writer state, and unresolved direct operation."""
        return coordinator.status(task_id)

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def open_direct_workspace(
        task_id: str,
        expected_revision: int,
        repository: str | None = None,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> DirectWorkspaceOpenResult:
        """Open/bind the local project for supervised ChatGPT development.

        Worktree mode is the default. This operation binds workspace identity to
        the durable supervised task but does not acquire the CHATGPT writer
        lease; acquire_chatgpt_workspace_writer remains the explicit single-
        writer gate before any patch.
        """
        if repository is not None and not repository.strip():
            raise tool_argument_error("repository must be omitted or non-empty")
        if base_ref is not None and not base_ref.strip():
            raise tool_argument_error("base_ref must be omitted or non-empty")
        return await coordinator.open(
            task_id,
            expected_revision,
            repository=repository,
            worktree=worktree,
            base_ref=base_ref,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def read_direct_workspace_file(
        task_id: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> DirectWorkspaceReadResult:
        """Read a file from the task's supervised local workspace.

        Reads are allowed while either ChatGPT or Codex is the active writer.
        They do not acquire a write lease or advance task revision.
        """
        if not path.strip():
            raise tool_argument_error("path must not be empty")
        if start_line is not None and start_line < 1:
            raise tool_argument_error("start_line must be >= 1")
        if end_line is not None and start_line is None:
            raise tool_argument_error("end_line requires start_line")
        if end_line is not None and start_line is not None and end_line < start_line:
            raise tool_argument_error("end_line must be >= start_line")
        return await coordinator.read(
            task_id,
            path,
            start_line=start_line,
            end_line=end_line,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def refresh_direct_git_state(task_id: str) -> GitState:
        """Read current Git branch/HEAD/changed-files through a fixed internal command.

        The model cannot supply arbitrary command text through this tool. The
        observed Git facts update derived workspace state without advancing the
        supervised task revision.
        """
        return await coordinator.refresh_git_state(task_id)

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def apply_direct_workspace_patch(
        task_id: str,
        expected_revision: int,
        expected_writer_epoch: int,
        patch: str,
    ) -> DirectWorkspacePatchResult:
        """Apply one patch as ChatGPT's direct-development writer.

        Requires the current CHATGPT writer lease. The operation uses a durable
        PREPARED -> external mutation -> Git/review observation -> finalize
        protocol. If intent/revision/writer state changes while the patch is in
        flight, the result is marked RECONCILIATION_REQUIRED and further writer
        transitions fail closed until the actual workspace is reconciled.
        """
        if not patch.strip():
            raise tool_argument_error("patch must not be empty")
        if expected_writer_epoch < 1:
            raise tool_argument_error("expected_writer_epoch must be >= 1")
        return await coordinator.apply_patch(
            task_id,
            expected_revision,
            expected_writer_epoch,
            patch,
        )
