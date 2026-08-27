from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.checkpoint_models import (
    CheckpointReviewDecision,
    CheckpointType,
)
from codex_supervisor_bridge.memory.checkpoint_reviews import (
    recommended_next_tool,
    review_checkpoint,
)
from codex_supervisor_bridge.memory.checkpoint_store import latest_checkpoint
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime
from codex_supervisor_bridge.memory.context_pack import ContextPackBuilder
from codex_supervisor_bridge.memory.errors import ConflictError, StaleRevisionError
from codex_supervisor_bridge.memory.models import EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService


class FakeProgressAdapter:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def __aenter__(self) -> "FakeProgressAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def get_operation_status(self, operation_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "operationId": operation_id,
            "threadId": "thread-1",
            "turnId": "turn-1",
            "status": self.state.get("status", "running"),
            "nextRecommendedAction": self.state.get("next_action", "wait_execution"),
            "progressEvents": list(self.state.get("events", [])),
            "lastError": self.state.get("last_error"),
        }

    async def get_workflow_status(self, workflow_id: str) -> dict[str, Any]:
        return {
            "ok": True,
            "workflowId": workflow_id,
            "status": self.state.get("status", "running"),
            "nextRecommendedAction": self.state.get("next_action", "wait_execution"),
        }

    async def list_pending_interactions(self, **_: Any) -> dict[str, Any]:
        return {"ok": True, "interactions": list(self.state.get("interactions", []))}


def make_checkpoint_service(
    memory: MemoryService,
    state: dict[str, Any],
    task_id: str = "CP-1",
) -> tuple[CheckpointService, int]:
    task = memory.create_task(task_id, "Checkpoint task", goal="Implement safely")
    task, _ = bind_codex_runtime(
        memory.store,
        task.task_id,
        task.revision,
        event_type=EventType.CODEX_STARTED,
        workflow_id="wf-1",
        operation_id="op-1",
        thread_id="thread-1",
        turn_id="turn-1",
        remote_status="running",
        next_action="wait_execution",
        task_phase=TaskPhase.IMPLEMENTING,
    )
    coordinator = CodexCoordinator(memory, lambda: FakeProgressAdapter(state))  # type: ignore[arg-type]
    return CheckpointService(memory, coordinator), task.revision


def test_heartbeat_is_derived_and_does_not_advance_revision() -> None:
    memory = MemoryService()
    state: dict[str, Any] = {"events": []}
    service, revision = make_checkpoint_service(memory, state)

    async def scenario() -> None:
        first = await service.collect("CP-1", force_heartbeat=True)
        assert first.created is True
        assert first.checkpoint.checkpoint_type == CheckpointType.HEARTBEAT
        assert first.checkpoint.requires_review is False
        assert first.task.revision == revision
        assert memory.get_task("CP-1").revision == revision

        second = await service.collect("CP-1", force_heartbeat=True)
        assert second.created is False
        assert second.checkpoint.checkpoint_id == first.checkpoint.checkpoint_id
        assert memory.get_task("CP-1").revision == revision

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_progress_checkpoint_advances_revision_and_context_hides_raw_delta() -> None:
    memory = MemoryService()
    state = {
        "events": [
            {
                "method": "reasoning/delta",
                "delta": "RAW-SECRET-REASONING-DELTA",
            },
            {
                "method": "item/completed",
                "message": "Implemented SaveManager and tests passed",
                "params": {"file": "src/save_manager.py"},
            },
        ]
    }
    service, revision = make_checkpoint_service(memory, state)

    async def scenario() -> None:
        result = await service.collect("CP-1")
        checkpoint = result.checkpoint
        assert result.created is True
        assert checkpoint.checkpoint_type == CheckpointType.PROGRESS
        assert checkpoint.requires_review is True
        assert result.task.revision == revision + 1
        assert result.task.phase == TaskPhase.SUPERVISOR_REVIEW
        assert "Implemented SaveManager" in checkpoint.completed[0]
        assert "src/save_manager.py" in checkpoint.files_changed
        assert checkpoint.validation["status"] == "passed"

        pack = ContextPackBuilder(memory.store).build("CP-1")
        assert "LATEST SUPERVISOR CHECKPOINT" in pack.content
        assert "Implemented SaveManager" in pack.content
        assert "src/save_manager.py" in pack.content
        assert "RAW-SECRET-REASONING-DELTA" not in pack.content

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_pending_interaction_or_validation_failure_is_gate_checkpoint() -> None:
    memory = MemoryService()
    state = {
        "events": [
            {
                "method": "item/failed",
                "message": "pytest failed in migration test",
            }
        ],
        "interactions": [
            {"interactionId": "ask-1", "kind": "approval", "status": "pending"}
        ],
    }
    service, _ = make_checkpoint_service(memory, state)

    async def scenario() -> None:
        result = await service.collect("CP-1")
        assert result.checkpoint.checkpoint_type == CheckpointType.GATE
        assert result.checkpoint.validation["status"] == "failed"
        assert result.checkpoint.blockers
        assert "approval" in " ".join(result.checkpoint.blockers).lower()
        assert result.checkpoint.requires_review is True

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_only_latest_unreviewed_checkpoint_can_be_reviewed() -> None:
    memory = MemoryService()
    state = {"events": [{"method": "item/completed", "message": "Step one complete"}]}
    service, _ = make_checkpoint_service(memory, state)

    async def scenario() -> None:
        first = await service.collect("CP-1")
        current = memory.get_task("CP-1")
        state["events"] = [{"method": "item/completed", "message": "Step two complete"}]
        state["next_action"] = "continue_step_two"
        second = await service.collect("CP-1")
        current = memory.get_task("CP-1")

        with pytest.raises(ConflictError, match="latest unreviewed"):
            review_checkpoint(
                memory.store,
                "CP-1",
                first.checkpoint.checkpoint_id,
                current.revision,
                CheckpointReviewDecision.CONTINUE,
            )

        task, review = review_checkpoint(
            memory.store,
            "CP-1",
            second.checkpoint.checkpoint_id,
            current.revision,
            CheckpointReviewDecision.CONTINUE,
        )
        assert review.decision == CheckpointReviewDecision.CONTINUE
        assert task.phase == TaskPhase.IMPLEMENTING
        assert task.revision == current.revision + 1
        assert recommended_next_tool(review.decision) is None

        with pytest.raises(StaleRevisionError):
            review_checkpoint(
                memory.store,
                "CP-1",
                second.checkpoint.checkpoint_id,
                current.revision,
                CheckpointReviewDecision.CONTINUE,
            )

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_steer_review_requires_instruction_and_returns_explicit_follow_up() -> None:
    memory = MemoryService()
    state = {"events": [{"method": "item/completed", "message": "Storage adapter complete"}]}
    service, _ = make_checkpoint_service(memory, state)

    async def scenario() -> None:
        result = await service.collect("CP-1")
        current = memory.get_task("CP-1")
        with pytest.raises(ValueError, match="requires a concrete instruction"):
            review_checkpoint(
                memory.store,
                "CP-1",
                result.checkpoint.checkpoint_id,
                current.revision,
                CheckpointReviewDecision.STEER,
            )
        task, review = review_checkpoint(
            memory.store,
            "CP-1",
            result.checkpoint.checkpoint_id,
            current.revision,
            CheckpointReviewDecision.STEER,
            instruction="Reuse the existing StorageManager abstraction.",
        )
        assert task.phase == TaskPhase.IMPLEMENTING
        assert recommended_next_tool(review.decision) == "soft_steer_codex"

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_latest_checkpoint_is_durable_across_reopen(tmp_path: Any) -> None:
    database = tmp_path / "checkpoints.db"
    state = {"events": [{"method": "item/completed", "message": "Durable step complete"}]}
    memory = MemoryService(database)
    service, _ = make_checkpoint_service(memory, state, task_id="CP-DURABLE")
    try:
        asyncio.run(service.collect("CP-DURABLE"))
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        checkpoint = latest_checkpoint(reopened.store, "CP-DURABLE")
        assert checkpoint is not None
        assert checkpoint.sequence == 1
        assert checkpoint.completed == ["Durable step complete"]
    finally:
        reopened.close()
