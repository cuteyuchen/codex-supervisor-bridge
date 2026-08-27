from __future__ import annotations

from typing import Any

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from codex_supervisor_bridge.memory.checkpoint_models import CheckpointReviewDecision
from codex_supervisor_bridge.memory.checkpoint_reviews import (
    get_checkpoint_review,
    recommended_next_tool,
    review_checkpoint,
)
from codex_supervisor_bridge.memory.checkpoint_store import latest_checkpoint, list_checkpoints
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService

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


def register_checkpoint_tools(
    server: MCPServer,
    memory: MemoryService,
    checkpoints: CheckpointService,
) -> None:
    @server.tool(annotations=MUTATION)
    @expose_integration_errors
    async def collect_codex_checkpoint(
        task_id: str,
        force_heartbeat: bool = False,
    ) -> dict[str, Any]:
        """Collect one bounded checkpoint from current Codex runtime facts.

        High-frequency raw deltas are filtered out. HEARTBEAT observations do
        not advance task revision; meaningful PROGRESS/GATE checkpoints do and
        require Supervisor review.
        """
        result = await checkpoints.collect(task_id, force_heartbeat=force_heartbeat)
        return result.model_dump(mode="json")

    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def get_latest_codex_checkpoint(task_id: str) -> dict[str, Any]:
        """Read the latest structured Codex checkpoint and its review, if any."""
        checkpoint = latest_checkpoint(memory.store, task_id)
        review = get_checkpoint_review(memory.store, checkpoint.checkpoint_id) if checkpoint else None
        return {
            "task": memory.get_task(task_id).model_dump(mode="json"),
            "checkpoint": checkpoint.model_dump(mode="json") if checkpoint else None,
            "review": review.model_dump(mode="json") if review else None,
        }

    @server.tool(annotations=READ_ONLY)
    @expose_memory_errors
    def list_codex_checkpoints(task_id: str, limit: int = 20) -> dict[str, Any]:
        """List recent structured checkpoints without returning raw Codex event streams."""
        if not 1 <= limit <= 100:
            raise tool_argument_error("limit must be between 1 and 100")
        items = list_checkpoints(memory.store, task_id, limit=limit)
        return {
            "task_id": task_id,
            "checkpoints": [item.model_dump(mode="json") for item in items],
        }

    @server.tool(annotations=MUTATION)
    @expose_memory_errors
    def review_codex_checkpoint(
        task_id: str,
        checkpoint_id: str,
        expected_revision: int,
        decision: CheckpointReviewDecision,
        instruction: str | None = None,
    ) -> dict[str, Any]:
        """Record the Supervisor decision for the latest unreviewed checkpoint.

        This records policy; it does not silently perform the next remote
        action. Use recommended_next_tool for the explicit follow-up call.
        """
        if decision == CheckpointReviewDecision.STEER and not (instruction or "").strip():
            raise tool_argument_error("STEER requires a concrete instruction")
        task, review = review_checkpoint(
            memory.store,
            task_id,
            checkpoint_id,
            expected_revision,
            decision,
            instruction=instruction,
        )
        return {
            "task": task.model_dump(mode="json"),
            "review": review.model_dump(mode="json"),
            "recommended_next_tool": recommended_next_tool(decision),
            "recommended_instruction": review.instruction,
        }
