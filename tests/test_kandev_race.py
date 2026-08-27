from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.memory.errors import StaleRevisionError
from codex_supervisor_bridge.memory.service import MemoryService


class RacingAdapter:
    def __init__(
        self,
        memory: MemoryService,
        task_id: str,
        initial_revision: int,
        state: dict[str, Any],
    ) -> None:
        self.memory = memory
        self.task_id = task_id
        self.initial_revision = initial_revision
        self.state = state

    async def __aenter__(self) -> "RacingAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def create_task(self, request: Any) -> dict[str, Any]:
        external_id = request.external_id
        self.state.setdefault("external_ids", []).append(external_id)
        self.state["calls"] = self.state.get("calls", 0) + 1
        if self.state["calls"] == 1:
            self.memory.record_user_override(
                self.task_id,
                self.initial_revision,
                "Newer user instruction arrived while Kandev create was in flight.",
            )
        return {"id": "ktask-stable", "external_id": external_id}


def test_remote_create_then_stale_local_bind_preserves_newer_user_revision() -> None:
    memory = MemoryService()
    task = memory.create_task("TASK-RACE", "Cross-system race")
    state: dict[str, Any] = {}
    coordinator = KandevCoordinator(
        memory,
        lambda: RacingAdapter(memory, task.task_id, task.revision, state),
    )

    async def scenario() -> None:
        with pytest.raises(StaleRevisionError):
            await coordinator.provision_task(task.task_id, task.revision)

        after_override = memory.get_task(task.task_id)
        assert after_override.revision == task.revision + 1
        assert after_override.external_kandev_task_id is None

        rebound = await coordinator.provision_task(task.task_id, after_override.revision)
        current = memory.get_task(task.task_id)
        assert rebound.kandev_task_id == "ktask-stable"
        assert current.external_kandev_task_id == "ktask-stable"
        assert current.revision == after_override.revision + 1
        assert state["calls"] == 2
        assert state["external_ids"] == [
            "codex-supervisor-bridge:TASK-RACE",
            "codex-supervisor-bridge:TASK-RACE",
        ]

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
