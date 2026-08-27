from __future__ import annotations

from mcp.server import MCPServer

from codex_supervisor_bridge.memory.models import ConstraintSeverity, ContextPackMode
from codex_supervisor_bridge.memory.service import MemoryService

from .models import (
    ConstraintResponse,
    ContextPackResponse,
    DecisionResponse,
    EventResponse,
    PlanResponse,
    SearchResponse,
    TaskResponse,
    TimelineResponse,
)


def register_memory_tools(server: MCPServer, service: MemoryService) -> None:
    """Register the ChatGPT-facing P2 tool surface.

    These tools are intentionally semantic. No shell, arbitrary filesystem,
    process execution, or raw SQL capability is exposed to the MCP caller.
    """

    @server.tool()
    def create_supervised_task(
        task_id: str,
        title: str,
        repository: str | None = None,
        goal: str | None = None,
        hard_constraints: list[str] | None = None,
    ) -> TaskResponse:
        """Create durable supervisor memory for a new development task.

        Use a stable human-readable task_id. Existing task IDs are rejected
        rather than overwritten. Initial hard constraints are recorded as
        first-class constraints and advance the returned task revision.
        """
        task = service.create_task(
            task_id,
            title,
            repository=repository,
            goal=goal,
            hard_constraints=hard_constraints,
        )
        return TaskResponse(task=task)

    @server.tool()
    def get_supervised_task(task_id: str) -> TaskResponse:
        """Read canonical lightweight task state and current version counters."""
        return TaskResponse(task=service.get_task(task_id))

    @server.tool()
    def resume_supervised_task(
        task_id: str,
        mode: ContextPackMode = ContextPackMode.RESUME,
    ) -> ContextPackResponse:
        """Resume supervision from durable memory without relying on prior chat history.

        This persists a Context Snapshot for audit/debug purposes but does not
        advance the task revision.
        """
        return ContextPackResponse.from_pack(service.resume_task(task_id, mode=mode))

    @server.tool()
    def get_context_pack(
        task_id: str,
        mode: ContextPackMode = ContextPackMode.RESUME,
    ) -> ContextPackResponse:
        """Build the current bounded Context Pack without persisting a snapshot."""
        return ContextPackResponse.from_pack(service.get_context_pack(task_id, mode=mode))

    @server.tool()
    def search_task_memory(
        task_id: str,
        query: str,
        limit: int = 10,
    ) -> SearchResponse:
        """Search current and historical task memory for evidence or past decisions.

        Search results include lifecycle status, so historical superseded
        decisions/plans can be inspected without treating them as current truth.
        """
        if not 1 <= limit <= 50:
            raise ValueError("limit must be between 1 and 50")
        hits = service.search_task_memory(task_id, query, limit=limit)
        return SearchResponse(task_id=task_id, query=query, hits=hits)

    @server.tool()
    def get_task_timeline(task_id: str, limit: int = 100) -> TimelineResponse:
        """Read the append-only supervisory event timeline for a task."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return TimelineResponse(
            task=service.get_task(task_id),
            events=service.timeline(task_id, limit=limit),
        )

    @server.tool()
    def record_user_override(
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> EventResponse:
        """Record the user's newest steering instruction as an immutable override event.

        This tool records the instruction but does not guess whether it changes
        architecture or intent. Use update_task_intent separately when the
        effective user goal itself changed.
        """
        event = service.record_user_override(task_id, expected_revision, instruction)
        return EventResponse(task=service.get_task(task_id), event=event)

    @server.tool()
    def update_task_intent(
        task_id: str,
        expected_revision: int,
        goal: str,
    ) -> TaskResponse:
        """Replace the current effective user goal and increment intent_version.

        Use only for a material intent change. This is revision-protected and
        stale supervisor calls are rejected with STALE_CONTEXT.
        """
        task = service.update_intent(task_id, expected_revision, goal)
        return TaskResponse(task=task)

    @server.tool()
    def add_task_decision(
        task_id: str,
        expected_revision: int,
        title: str,
        content: str,
        decision_type: str = "general",
    ) -> DecisionResponse:
        """Add an ACTIVE supervisor decision to canonical task memory."""
        decision = service.add_decision(
            task_id,
            expected_revision,
            title,
            content,
            decision_type=decision_type,
        )
        return DecisionResponse(task=service.get_task(task_id), decision=decision)

    @server.tool()
    def supersede_task_decision(
        task_id: str,
        expected_revision: int,
        decision_id: str,
        superseded_by: str | None = None,
    ) -> DecisionResponse:
        """Mark an ACTIVE decision SUPERSEDED while preserving it as history."""
        decision = service.supersede_decision(
            task_id,
            expected_revision,
            decision_id,
            superseded_by=superseded_by,
        )
        return DecisionResponse(task=service.get_task(task_id), decision=decision)

    @server.tool()
    def add_task_constraint(
        task_id: str,
        expected_revision: int,
        content: str,
        scope: str = "task",
        severity: ConstraintSeverity = ConstraintSeverity.HARD,
    ) -> ConstraintResponse:
        """Add an ACTIVE user constraint.

        HARD constraints have mandatory priority in Context Packs and are never
        silently dropped to satisfy optional context budget limits.
        """
        constraint = service.add_constraint(
            task_id,
            expected_revision,
            content,
            scope=scope,
            severity=severity,
        )
        return ConstraintResponse(task=service.get_task(task_id), constraint=constraint)

    @server.tool()
    def supersede_task_constraint(
        task_id: str,
        expected_revision: int,
        constraint_id: str,
        superseded_by: str | None = None,
    ) -> ConstraintResponse:
        """Mark an ACTIVE constraint SUPERSEDED without deleting its history."""
        constraint = service.supersede_constraint(
            task_id,
            expected_revision,
            constraint_id,
            superseded_by=superseded_by,
        )
        return ConstraintResponse(task=service.get_task(task_id), constraint=constraint)

    @server.tool()
    def create_task_plan(
        task_id: str,
        expected_revision: int,
        content: str,
    ) -> PlanResponse:
        """Create a new DRAFT plan version and move the task to plan review."""
        plan = service.create_plan(task_id, expected_revision, content)
        return PlanResponse(task=service.get_task(task_id), plan=plan)

    @server.tool()
    def get_current_plan(task_id: str, approved_only: bool = False) -> PlanResponse:
        """Read the latest plan or only the currently approved plan."""
        plan = service.approved_plan(task_id) if approved_only else service.latest_plan(task_id)
        return PlanResponse(task=service.get_task(task_id), plan=plan)

    @server.tool()
    def approve_task_plan(
        task_id: str,
        expected_revision: int,
        plan_id: str,
    ) -> PlanResponse:
        """Approve a DRAFT plan and make implementation the active phase."""
        plan = service.approve_plan(task_id, expected_revision, plan_id)
        return PlanResponse(task=service.get_task(task_id), plan=plan)

    @server.tool()
    def reject_task_plan(
        task_id: str,
        expected_revision: int,
        plan_id: str,
        reason: str,
    ) -> PlanResponse:
        """Reject a DRAFT plan and return the task to planning."""
        plan = service.reject_plan(
            task_id,
            expected_revision,
            plan_id,
            reason=reason,
        )
        return PlanResponse(task=service.get_task(task_id), plan=plan)
