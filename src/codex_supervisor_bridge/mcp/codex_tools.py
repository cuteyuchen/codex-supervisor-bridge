from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator

from .errors import expose_integration_errors, tool_argument_error

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

INTERRUPT = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)


def register_codex_tools(server: MCPServer, coordinator: CodexCoordinator) -> None:
    """Register the P4 semantic Codex control surface.

    Raw upstream tools are intentionally not re-exported. The Supervisor can
    start/read a Plan workflow, import and approve a reviewed plan, execute it,
    steer the current turn, answer bounded interactions, or interrupt work.
    """

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_codex_control_health() -> dict[str, Any]:
        """Verify Codex Control Plane MCP contract compatibility and health."""
        return await coordinator.health()

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_codex_runtime_capabilities() -> dict[str, Any]:
        """Read sanitized Codex runtime capabilities after contract verification."""
        return await coordinator.runtime_capabilities()

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def preflight_codex_project(
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]:
        """Run passive Codex preflight checks without starting a live probe."""
        if workflow_kind not in {"plan", "write", "review"}:
            raise tool_argument_error("workflow_kind must be plan, write, or review")
        return await coordinator.preflight(
            project_id=project_id,
            cwd=cwd,
            model=model,
            workflow_kind=workflow_kind,
        )

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def start_codex_plan(
        task_id: str,
        expected_revision: int,
        project_id: str,
        cwd: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """Start a durable Codex Plan Mode workflow in read-only sandbox.

        This never starts implementation. The returned workflow must be polled,
        its valid latestPlan imported into Supervisor memory, and that local
        plan explicitly approved before execute_codex_approved_plan is allowed.
        """
        if not project_id.strip():
            raise tool_argument_error("project_id must not be empty")
        return await coordinator.start_plan(
            task_id,
            expected_revision,
            project_id=project_id,
            cwd=cwd,
            model=model,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def get_codex_status(task_id: str) -> dict[str, Any]:
        """Read current durable Codex workflow/operation status without changing revision."""
        return await coordinator.status(task_id)

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def import_codex_plan(
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Import Codex latestPlan as a local DRAFT plan for Supervisor review.

        Only upstream plans classified as valid_plan are accepted. This does not
        approve or execute the plan.
        """
        return await coordinator.import_latest_plan(task_id, expected_revision)

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def execute_codex_approved_plan(
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Execute the locally APPROVED plan in workspace-write sandbox.

        Before execution, the Bridge re-reads Codex latestPlan and refuses to
        run if it differs from the local approved plan.
        """
        return await coordinator.execute_approved_plan(
            task_id,
            expected_revision,
            sandbox="workspace-write",
        )

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def soft_steer_codex(
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> dict[str, Any]:
        """Add an instruction to the current active Codex turn via turn/steer.

        This targets the persisted current thread/turn and does not create a
        second turn. Use interrupt_codex + a new plan for architecture/scope
        changes instead of forcing a hard replan through soft steering.
        """
        if not instruction.strip():
            raise tool_argument_error("instruction must not be empty")
        return await coordinator.soft_steer(task_id, expected_revision, instruction)

    @server.tool(annotations=INTERRUPT)
    @expose_integration_errors
    async def interrupt_codex(
        task_id: str,
        expected_revision: int,
        reason: str | None = None,
    ) -> dict[str, Any]:
        """Interrupt the current Codex turn and move the Supervisor task to PAUSED."""
        return await coordinator.interrupt(
            task_id,
            expected_revision,
            reason=reason,
        )

    @server.tool(annotations=READ_ONLY)
    @expose_integration_errors
    async def list_codex_pending_interactions(task_id: str) -> dict[str, Any]:
        """List pending Codex approvals/questions scoped to this supervised task."""
        return await coordinator.pending_interactions(task_id)

    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def answer_codex_pending_interaction(
        task_id: str,
        expected_revision: int,
        interaction_id: str,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        """Answer one pending Codex approval/question and record the control event."""
        if decision not in {None, "accept", "acceptForSession", "decline", "cancel"}:
            raise tool_argument_error("unsupported interaction decision")
        if scope not in {"turn", "session"}:
            raise tool_argument_error("scope must be turn or session")
        if decision is None and answers is None:
            raise tool_argument_error("decision or answers is required")
        return await coordinator.answer_interaction(
            task_id,
            expected_revision,
            interaction_id,
            decision=decision,
            answers=answers,
            scope=scope,
        )
