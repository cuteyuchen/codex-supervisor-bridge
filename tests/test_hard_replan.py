from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.integrations.codex_control_errors import CodexToolError
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.checkpoint_models import CheckpointType
from codex_supervisor_bridge.memory.checkpoint_store import create_checkpoint
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime
from codex_supervisor_bridge.memory.context_pack import ContextPackBuilder
from codex_supervisor_bridge.memory.models import EventType, PlanStatus, TaskPhase
from codex_supervisor_bridge.memory.replan_models import (
    HardReplanStatus,
    SnapshotClassificationStatus,
)
from codex_supervisor_bridge.memory.replans import active_hard_replan, latest_work_snapshot
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.replans import HardReplanService


class InterruptAdapter:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state

    async def __aenter__(self) -> "InterruptAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def interrupt(self, **identifiers: Any) -> dict[str, Any]:
        self.state.setdefault("interrupt_calls", []).append(identifiers)
        if self.state.get("fail_interrupt"):
            raise CodexToolError(
                "codex_interrupt_turn",
                "INTERRUPT_FAILED",
                "simulated interrupt failure",
            )
        return {"ok": True, "status": "interrupted", **identifiers}


def prepare_running_task(memory: MemoryService, task_id: str = "REPLAN-1") -> int:
    task = memory.create_task(
        task_id,
        "Hard replan task",
        repository="cuteyuchen/game",
        goal="Implement three save slots.",
    )
    plan = memory.create_plan(task.task_id, task.revision, "Build a three-slot save system.")
    task = memory.get_task(task.task_id)
    memory.approve_plan(task.task_id, task.revision, plan.plan_id)
    task = memory.get_task(task.task_id)
    task, _ = bind_codex_runtime(
        memory.store,
        task.task_id,
        task.revision,
        event_type=EventType.CODEX_STARTED,
        workflow_id="wf-old",
        operation_id="op-old",
        thread_id="thread-old",
        turn_id="turn-old",
        remote_status="running",
        next_action="wait_execution",
        task_phase=TaskPhase.IMPLEMENTING,
    )
    cp = create_checkpoint(
        memory.store,
        task.task_id,
        task.revision,
        checkpoint_type=CheckpointType.HEARTBEAT,
        source_fingerprint=f"baseline-{task_id}",
        trigger_reason="baseline",
        runtime={
            "workflow_id": "wf-old",
            "operation_id": "op-old",
            "thread_id": "thread-old",
            "turn_id": "turn-old",
            "remote_status": "running",
            "next_action": "wait_execution",
        },
        files_changed=["src/save_manager.py", "tests/test_save.py"],
        validation={"status": "passed", "tests": 12},
        evidence_refs=["codex-operation:op-old"],
    )
    return cp.task.revision


def test_hard_override_freezes_old_state_supersedes_plan_and_requires_classification() -> None:
    memory = MemoryService()
    state: dict[str, Any] = {}
    revision = prepare_running_task(memory)
    coordinator = CodexCoordinator(memory, lambda: InterruptAdapter(state))  # type: ignore[arg-type]
    service = HardReplanService(memory, coordinator)

    async def scenario() -> None:
        result = await service.begin(
            "REPLAN-1",
            revision,
            new_goal="Use exactly one save and reuse StorageManager.",
            reason="The multi-slot architecture is wrong.",
        )
        task = memory.get_task("REPLAN-1")
        assert result["interrupt_succeeded"] is True
        assert task.intent_version == 2
        assert task.phase == TaskPhase.PAUSED
        assert task.current_goal == "Use exactly one save and reuse StorageManager."
        assert memory.approved_plan("REPLAN-1") is None
        assert memory.latest_plan("REPLAN-1").status == PlanStatus.SUPERSEDED

        replan = active_hard_replan(memory.store, "REPLAN-1")
        snapshot = latest_work_snapshot(memory.store, "REPLAN-1")
        assert replan is not None and replan.status == HardReplanStatus.SNAPSHOT_READY
        assert snapshot is not None
        assert snapshot.goal == "Implement three save slots."
        assert snapshot.approved_plan_id is not None
        assert snapshot.codex_workflow_id == "wf-old"
        assert snapshot.operation_id == "op-old"
        assert snapshot.thread_id == "thread-old"
        assert snapshot.turn_id == "turn-old"
        assert snapshot.changed_files == ["src/save_manager.py", "tests/test_save.py"]
        assert snapshot.validation["status"] == "passed"
        assert snapshot.classification_status == SnapshotClassificationStatus.UNCLASSIFIED
        assert state["interrupt_calls"][-1]["workflow_id"] == "wf-old"

        classified = service.classify(
            "REPLAN-1",
            snapshot.snapshot_id,
            task.revision,
            keep=["src/save_manager.py"],
            modify=["tests/test_save.py"],
            drop=["legacy SaveSlotSelector"],
            notes="Keep reusable storage work; remove only through the new approved plan.",
        )
        latest_task = memory.get_task("REPLAN-1")
        assert latest_task.phase == TaskPhase.REPLANNING
        assert classified["replan"]["status"] == HardReplanStatus.READY_TO_PLAN.value
        assert classified["classification_decision"]["decision_type"] == (
            "work_snapshot_classification"
        )
        assert latest_task.revision == task.revision + 2

        pack = ContextPackBuilder(memory.store).build("REPLAN-1")
        assert "Hard replan work disposition" in pack.content
        assert "src/save_manager.py" in pack.content
        assert "legacy SaveSlotSelector" in pack.content
        assert "planning metadata only" in pack.content

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_interrupt_failure_blocks_then_revision_protected_retry_recovers() -> None:
    memory = MemoryService()
    state: dict[str, Any] = {"fail_interrupt": True}
    revision = prepare_running_task(memory, "REPLAN-FAIL")
    coordinator = CodexCoordinator(memory, lambda: InterruptAdapter(state))  # type: ignore[arg-type]
    service = HardReplanService(memory, coordinator)

    async def scenario() -> None:
        first = await service.begin(
            "REPLAN-FAIL",
            revision,
            new_goal="Switch to a single-save design.",
            reason="Architecture changed.",
        )
        blocked = memory.get_task("REPLAN-FAIL")
        assert first["interrupt_succeeded"] is False
        assert blocked.phase == TaskPhase.BLOCKED
        assert first["replan"]["status"] == HardReplanStatus.INTERRUPT_FAILED.value
        assert first["recommended_next_tool"] == "retry_hard_replan_interrupt"

        state["fail_interrupt"] = False
        retry = await service.retry_interrupt(
            "REPLAN-FAIL",
            blocked.revision,
            first["replan"]["replan_id"],
        )
        recovered = memory.get_task("REPLAN-FAIL")
        assert retry["interrupt_succeeded"] is True
        assert retry["replan"]["status"] == HardReplanStatus.SNAPSHOT_READY.value
        assert recovered.phase == TaskPhase.PAUSED
        assert recovered.revision == blocked.revision + 2
        assert len(state["interrupt_calls"]) == 2

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_keep_modify_drop_must_be_disjoint() -> None:
    memory = MemoryService()
    state: dict[str, Any] = {}
    revision = prepare_running_task(memory, "REPLAN-OVERLAP")
    service = HardReplanService(
        memory,
        CodexCoordinator(memory, lambda: InterruptAdapter(state)),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        result = await service.begin(
            "REPLAN-OVERLAP",
            revision,
            new_goal="New architecture",
            reason="Old architecture invalid",
        )
        with pytest.raises(ValueError, match="must be disjoint"):
            service.classify(
                "REPLAN-OVERLAP",
                result["snapshot"]["snapshot_id"],
                result["task"]["revision"],
                keep=["same-item"],
                modify=["same-item"],
                drop=[],
            )

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_next_hard_replan_supersedes_old_snapshot_classification_decision() -> None:
    memory = MemoryService()
    state: dict[str, Any] = {}
    revision = prepare_running_task(memory, "REPLAN-TWICE")
    service = HardReplanService(
        memory,
        CodexCoordinator(memory, lambda: InterruptAdapter(state)),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        first = await service.begin(
            "REPLAN-TWICE",
            revision,
            new_goal="First new goal",
            reason="First hard replan",
        )
        classified = service.classify(
            "REPLAN-TWICE",
            first["snapshot"]["snapshot_id"],
            first["task"]["revision"],
            keep=["A"],
            modify=[],
            drop=["B"],
        )
        decision_id = classified["classification_decision"]["decision_id"]
        current = memory.get_task("REPLAN-TWICE")

        second = await service.begin(
            "REPLAN-TWICE",
            current.revision,
            new_goal="Second new goal",
            reason="User changed direction again before replanning.",
        )
        assert second["task"]["intent_version"] == 3
        active_ids = {item.decision_id for item in memory.store.active_decisions("REPLAN-TWICE")}
        assert decision_id not in active_ids
        assert second["replan"]["status"] == HardReplanStatus.SNAPSHOT_READY.value

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
