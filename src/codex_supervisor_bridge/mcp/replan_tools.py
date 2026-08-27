from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.supervisor.replans import HardReplanService

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


def register_replan_tools(server: MCPServer, replans: HardReplanService) -> None:
    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def begin_hard_replan(
        task_id: str,
        expected_revision: int,
        new_goal: str,
        reason: str,
    ) -> dict[str, Any]:
        """Record a material user intent change, freeze work state, and interrupt Codex.

        The new intent and snapshot are persisted before the remote interrupt.
        Existing DRAFT/APPROVED plans are superseded. The task remains PAUSED
        until the snapshot is explicitly classified KEEP/MODIFY/DROP.
        """
        if not new_goal.strip():
            raise tool_argument_error("new_goal must not be empty")
        if not reason.strip():
            raise tool_argument_error("reason must not be empty")
        return await replans.begin(
            task_id,
            expected_revision,
            new_goal=new_goal,
            reason=reason,
        )

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def retry_hard_replan_interrupt(
        task_id: str,
        expected_revision: int,
        replan_id: str,
    ) -> dict[str, Any]:
        """Retry a previously failed hard-replan interrupt under a fresh revision lock."""
        return await replans.retry_interrupt(task_id, expected_revision, replan_id)

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def classify_work_snapshot(
        task_id: str,
        snapshot_id: str,
        expected_revision: int,
        keep: list[str],
        modify: list[str],
        drop: list[str],
        notes: str | None = None,
    ) -> dict[str, Any]:
        """Classify frozen partial work as KEEP / MODIFY / DROP before replanning.

        This is planning metadata only. It does not delete, reset, checkout, or
        otherwise mutate repository files. The three lists must be disjoint.
        """
        return replans.classify(
            task_id,
            snapshot_id,
            expected_revision,
            keep=keep,
            modify=modify,
            drop=drop,
            notes=notes,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def get_hard_replan_state(task_id: str) -> dict[str, Any]:
        """Read the active hard replan and latest work snapshot without mutation."""
        return replans.state(task_id)
