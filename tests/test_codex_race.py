from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.integrations.codex_control_errors import CodexCompensationError
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.codex_runtime import get_codex_runtime
from codex_supervisor_bridge.memory.errors import StaleRevisionError
from codex_supervisor_bridge.memory.service import MemoryService


class StartPlanRaceAdapter:
    def __init__(
        self,
        memory: MemoryService,
        task_id: str,
        expected_revision: int,
        state: dict[str, Any],
        *,
        interrupt_fails: bool = False,
    ) -> None:
        self.memory = memory
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.state = state
        self.interrupt_fails = interrupt_fails

    async def __aenter__(self) -> "StartPlanRaceAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def start_plan_workflow(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self.state["start_arguments"] = arguments
        if not self.state.get("override_recorded"):
            self.memory.record_user_override(
                self.task_id,
                self.expected_revision,
                "Stop using the old plan; user intent changed while Codex was starting.",
            )
            self.state["override_recorded"] = True
        return {
            "ok": True,
            "workflowId": "cwf-race",
            "threadId": "thread-race",
            "planTurnId": "turn-race",
            "planOperationId": "op-race",
            "status": "planning",
        }

    async def interrupt(
        self,
        *,
        workflow_id: str | None = None,
        operation_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        self.state["interrupt"] = {
            "workflow_id": workflow_id,
            "operation_id": operation_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }
        if self.interrupt_fails:
            raise RuntimeError("simulated compensation failure")
        return {"ok": True, "status": "interrupted"}


def test_user_revision_race_interrupts_remote_plan_and_preserves_newer_override() -> None:
    memory = MemoryService()
    task = memory.create_task("RACE-PLAN", "Plan race", goal="Original goal")
    state: dict[str, Any] = {}

    def factory() -> StartPlanRaceAdapter:
        return StartPlanRaceAdapter(memory, task.task_id, task.revision, state)

    coordinator = CodexCoordinator(memory, factory)  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(StaleRevisionError, match="STALE_CONTEXT"):
            await coordinator.start_plan(
                task.task_id,
                task.revision,
                project_id="project-race",
            )

    try:
        asyncio.run(scenario())
        current = memory.get_task(task.task_id)
        assert current.revision == task.revision + 1
        assert get_codex_runtime(memory.store, task.task_id) is None
        assert state["interrupt"] == {
            "workflow_id": "cwf-race",
            "operation_id": "op-race",
            "thread_id": "thread-race",
            "turn_id": "turn-race",
        }
        assert memory.timeline(task.task_id)[-1].payload["instruction"].startswith(
            "Stop using the old plan"
        )
    finally:
        memory.close()


def test_failed_stale_compensation_escalates_instead_of_claiming_safe_stop() -> None:
    memory = MemoryService()
    task = memory.create_task("RACE-COMP", "Compensation race", goal="Original goal")
    state: dict[str, Any] = {}

    def factory() -> StartPlanRaceAdapter:
        return StartPlanRaceAdapter(
            memory,
            task.task_id,
            task.revision,
            state,
            interrupt_fails=True,
        )

    coordinator = CodexCoordinator(memory, factory)  # type: ignore[arg-type]

    async def scenario() -> None:
        with pytest.raises(CodexCompensationError, match="CODEX_COMPENSATION_REQUIRED"):
            await coordinator.start_plan(
                task.task_id,
                task.revision,
                project_id="project-race",
            )

    try:
        asyncio.run(scenario())
        assert state["interrupt"]["workflow_id"] == "cwf-race"
        assert memory.get_task(task.task_id).revision == task.revision + 1
        assert get_codex_runtime(memory.store, task.task_id) is None
    finally:
        memory.close()
