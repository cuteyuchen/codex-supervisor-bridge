from __future__ import annotations

from typing import Any

from codex_supervisor_bridge.integrations.codex_control_errors import CodexControlError
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.replan_bindings import prepare_interrupt_retry
from codex_supervisor_bridge.memory.replan_models import HardReplanStatus
from codex_supervisor_bridge.memory.replans import (
    active_hard_replan,
    begin_hard_replan,
    classify_work_snapshot,
    finalize_interrupt,
    get_work_snapshot,
    latest_work_snapshot,
)
from codex_supervisor_bridge.memory.service import MemoryService


class HardReplanService:
    def __init__(self, memory: MemoryService, codex: CodexCoordinator) -> None:
        self.memory = memory
        self.codex = codex

    async def begin(
        self,
        task_id: str,
        expected_revision: int,
        *,
        new_goal: str,
        reason: str,
    ) -> dict[str, Any]:
        prepared = begin_hard_replan(
            self.memory.store,
            task_id,
            expected_revision,
            new_goal=new_goal,
            reason=reason,
        )
        snapshot = prepared.snapshot
        identifiers = {
            "workflow_id": snapshot.codex_workflow_id,
            "operation_id": snapshot.operation_id,
            "thread_id": snapshot.thread_id,
            "turn_id": snapshot.turn_id,
        }
        if not any(identifiers.values()):
            final = finalize_interrupt(
                self.memory.store,
                task_id,
                prepared.replan.replan_id,
                prepared.task.revision,
                succeeded=True,
            )
            return {
                **final.model_dump(mode="json"),
                "interrupt_succeeded": True,
                "interrupt_required": False,
                "recommended_next_tool": "classify_work_snapshot",
            }

        try:
            async with self.codex.adapter_factory() as adapter:
                remote = await adapter.interrupt(**identifiers)
        except CodexControlError as exc:
            final = finalize_interrupt(
                self.memory.store,
                task_id,
                prepared.replan.replan_id,
                prepared.task.revision,
                succeeded=False,
                error=str(exc),
            )
            return {
                **final.model_dump(mode="json"),
                "interrupt_succeeded": False,
                "interrupt_required": True,
                "remote": None,
                "recommended_next_tool": "retry_hard_replan_interrupt",
            }
        except Exception:
            finalize_interrupt(
                self.memory.store,
                task_id,
                prepared.replan.replan_id,
                prepared.task.revision,
                succeeded=False,
                error="unexpected remote interrupt failure",
            )
            raise

        final = finalize_interrupt(
            self.memory.store,
            task_id,
            prepared.replan.replan_id,
            prepared.task.revision,
            succeeded=True,
        )
        return {
            **final.model_dump(mode="json"),
            "interrupt_succeeded": True,
            "interrupt_required": True,
            "remote": remote,
            "recommended_next_tool": "classify_work_snapshot",
        }

    async def retry_interrupt(
        self,
        task_id: str,
        expected_revision: int,
        replan_id: str,
    ) -> dict[str, Any]:
        retry_task, replan, snapshot = prepare_interrupt_retry(
            self.memory.store,
            task_id,
            replan_id,
            expected_revision,
        )
        identifiers = {
            "workflow_id": snapshot.codex_workflow_id,
            "operation_id": snapshot.operation_id,
            "thread_id": snapshot.thread_id,
            "turn_id": snapshot.turn_id,
        }
        if not any(identifiers.values()):
            final = finalize_interrupt(
                self.memory.store,
                task_id,
                replan.replan_id,
                retry_task.revision,
                succeeded=True,
            )
            return {
                **final.model_dump(mode="json"),
                "interrupt_succeeded": True,
                "recommended_next_tool": "classify_work_snapshot",
            }
        try:
            async with self.codex.adapter_factory() as adapter:
                remote = await adapter.interrupt(**identifiers)
        except CodexControlError as exc:
            final = finalize_interrupt(
                self.memory.store,
                task_id,
                replan.replan_id,
                retry_task.revision,
                succeeded=False,
                error=str(exc),
            )
            return {
                **final.model_dump(mode="json"),
                "interrupt_succeeded": False,
                "remote": None,
                "recommended_next_tool": "retry_hard_replan_interrupt",
            }
        except Exception:
            finalize_interrupt(
                self.memory.store,
                task_id,
                replan.replan_id,
                retry_task.revision,
                succeeded=False,
                error="unexpected remote interrupt retry failure",
            )
            raise
        final = finalize_interrupt(
            self.memory.store,
            task_id,
            replan.replan_id,
            retry_task.revision,
            succeeded=True,
        )
        return {
            **final.model_dump(mode="json"),
            "interrupt_succeeded": True,
            "remote": remote,
            "recommended_next_tool": "classify_work_snapshot",
        }

    def classify(
        self,
        task_id: str,
        snapshot_id: str,
        expected_revision: int,
        *,
        keep: list[str],
        modify: list[str],
        drop: list[str],
        notes: str | None = None,
    ) -> dict[str, Any]:
        result = classify_work_snapshot(
            self.memory.store,
            task_id,
            snapshot_id,
            expected_revision,
            keep=keep,
            modify=modify,
            drop=drop,
            notes=notes,
        )
        return {
            **result.model_dump(mode="json"),
            "recommended_next_tool": "start_codex_plan",
        }

    def state(self, task_id: str) -> dict[str, Any]:
        task = self.memory.get_task(task_id)
        replan = active_hard_replan(self.memory.store, task_id)
        snapshot = (
            get_work_snapshot(self.memory.store, replan.snapshot_id)
            if replan is not None
            else latest_work_snapshot(self.memory.store, task_id)
        )
        return {
            "task": task.model_dump(mode="json"),
            "replan": replan.model_dump(mode="json") if replan else None,
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        }
