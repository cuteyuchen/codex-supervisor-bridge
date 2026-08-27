from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.integrations.kandev_coordinator import (
    KandevCoordinator,
    KandevProvisionOptions,
)
from codex_supervisor_bridge.integrations.kandev_models import KandevCapabilities

from .errors import expose_integration_errors, tool_argument_error
from .models import KandevProvisionResponse

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


def register_kandev_tools(server: MCPServer, coordinator: KandevCoordinator) -> None:
    """Expose only the P3-safe Kandev subset to ChatGPT.

    Raw workflow moves, task state updates, agent start, and autopilot remain
    internal until the supervisor state machine and Codex live-control layer
    are connected in later phases.
    """

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_kandev_capabilities() -> KandevCapabilities:
        """Inspect whether the configured Kandev external MCP has the required P3 tools."""
        return await coordinator.capabilities()

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def provision_kandev_task(
        task_id: str,
        expected_revision: int,
        workspace_id: str | None = None,
        workflow_id: str | None = None,
        workflow_step_id: str | None = None,
        workspace_mode: str | None = None,
        agent_profile_id: str | None = None,
        executor_profile_id: str | None = None,
        repository_id: str | None = None,
        local_path: str | None = None,
        repository_url: str | None = None,
        base_branch: str | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
        start_when_unblocked: bool | None = None,
    ) -> KandevProvisionResponse:
        """Create/reuse and bind a Kandev task without starting an agent.

        The call is protected by expected_revision. Kandev receives a stable
        external_id so retries are idempotent even if the bridge rejects the
        local binding because a newer user override arrived during the remote
        call. P3 always forces start_agent=false and autopilot=false.
        """
        options = KandevProvisionOptions(
            parent_id=parent_id,
            workspace_id=workspace_id,
            workflow_id=workflow_id,
            workflow_step_id=workflow_step_id,
            workspace_mode=workspace_mode,
            agent_profile_id=agent_profile_id,
            executor_profile_id=executor_profile_id,
            repository_id=repository_id,
            local_path=local_path,
            repository_url=repository_url,
            base_branch=base_branch,
            blocked_by=blocked_by or [],
            start_when_unblocked=start_when_unblocked,
        )
        binding = await coordinator.provision_task(
            task_id,
            expected_revision,
            options=options,
        )
        return KandevProvisionResponse(
            task=coordinator.memory.get_task(task_id),
            binding=binding,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_kandev_sessions(task_id: str) -> dict[str, Any]:
        """Read Kandev sessions for the supervisor task's bound Kandev task."""
        return await coordinator.list_linked_sessions(task_id)

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_kandev_conversation(
        task_id: str,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """Read the bound Kandev task conversation for supervision/inspection."""
        if limit is not None and not 1 <= limit <= 1000:
            raise tool_argument_error("limit must be between 1 and 1000")
        return await coordinator.get_linked_conversation(
            task_id,
            session_id=session_id,
            limit=limit,
        )
