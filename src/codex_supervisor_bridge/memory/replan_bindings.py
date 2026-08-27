from __future__ import annotations

from .errors import ConflictError
from .models import TaskMemory, TaskPhase, utcnow
from .replan_models import HardReplan, HardReplanStatus, WorkSnapshot
from .replans import get_work_snapshot, replan_from_row
from .store import MemoryStore, _iso


def prepare_interrupt_retry(
    store: MemoryStore,
    task_id: str,
    replan_id: str,
    expected_revision: int,
) -> tuple[TaskMemory, HardReplan, WorkSnapshot]:
    """Revision-protected retry decision before another remote interrupt attempt."""
    now = _iso(utcnow())
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        row = conn.execute(
            "SELECT * FROM hard_replans WHERE replan_id = ? AND task_id = ?",
            (replan_id, task_id),
        ).fetchone()
        if row is None:
            raise ConflictError(f"Unknown hard replan for task {task_id}: {replan_id}")
        replan = replan_from_row(row)
        if replan.status != HardReplanStatus.INTERRUPT_FAILED:
            raise ConflictError(
                f"Hard replan interrupt retry is invalid from {replan.status.value}"
            )
        conn.execute(
            "UPDATE hard_replans SET status = ?, interrupt_error = NULL, updated_at = ? "
            "WHERE replan_id = ?",
            (HardReplanStatus.INTERRUPT_PENDING.value, now, replan_id),
        )
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={
                "phase": TaskPhase.PAUSED,
                "current_state": "Retrying Codex interrupt for hard replan.",
            },
        )
        snapshot = get_work_snapshot(store, replan.snapshot_id)
        if snapshot is None:
            raise ConflictError(f"Missing work snapshot: {replan.snapshot_id}")
        updated = replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (replan_id,),
            ).fetchone()
        )
        return task, updated, snapshot


def bind_replan_workflow(
    store: MemoryStore,
    task_id: str,
    workflow_id: str,
) -> HardReplan | None:
    """Bind the new read-only Codex plan workflow without task revision churn."""
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM hard_replans WHERE task_id = ? AND status = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id, HardReplanStatus.READY_TO_PLAN.value),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE hard_replans SET status = ?, new_workflow_id = ?, updated_at = ? "
            "WHERE replan_id = ?",
            (HardReplanStatus.PLANNING.value, workflow_id, now, row["replan_id"]),
        )
        return replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (row["replan_id"],),
            ).fetchone()
        )


def bind_replan_plan_review(
    store: MemoryStore,
    task_id: str,
    plan_id: str,
) -> HardReplan | None:
    """Attach the newly imported local DRAFT plan to the active hard replan."""
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM hard_replans WHERE task_id = ? AND status IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (
                task_id,
                HardReplanStatus.PLANNING.value,
                HardReplanStatus.READY_TO_PLAN.value,
            ),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE hard_replans SET status = ?, new_plan_id = ?, updated_at = ? "
            "WHERE replan_id = ?",
            (HardReplanStatus.PLAN_REVIEW.value, plan_id, now, row["replan_id"]),
        )
        return replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (row["replan_id"],),
            ).fetchone()
        )


def complete_replan_on_execution(
    store: MemoryStore,
    task_id: str,
    plan_id: str,
) -> HardReplan | None:
    """Close the replan when its reviewed replacement plan begins execution."""
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM hard_replans WHERE task_id = ? AND status = ? AND new_plan_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id, HardReplanStatus.PLAN_REVIEW.value, plan_id),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE hard_replans SET status = ?, updated_at = ? WHERE replan_id = ?",
            (HardReplanStatus.COMPLETED.value, now, row["replan_id"]),
        )
        return replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (row["replan_id"],),
            ).fetchone()
        )
