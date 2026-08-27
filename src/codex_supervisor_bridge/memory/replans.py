from __future__ import annotations

import json
from typing import Any

from .errors import ConflictError
from .models import Actor, EventType, PlanStatus, TaskPhase, utcnow
from .replan_models import (
    HardReplan,
    HardReplanBeginResult,
    HardReplanStatus,
    SnapshotClassificationStatus,
    WorkSnapshot,
)
from .store import MemoryStore, _dt, _id, _iso, _json


def _load_list(value: str | None) -> list[str]:
    if not value:
        return []
    decoded = json.loads(value)
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _load_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def snapshot_from_row(row: Any) -> WorkSnapshot:
    return WorkSnapshot(
        snapshot_id=row["snapshot_id"],
        task_id=row["task_id"],
        captured_revision=row["captured_revision"],
        intent_version=row["intent_version"],
        plan_version=row["plan_version"],
        goal=row["goal"],
        phase=row["phase"],
        approved_plan_id=row["approved_plan_id"],
        kandev_task_id=row["kandev_task_id"],
        git_branch=row["git_branch"],
        git_head=row["git_head"],
        checkpoint_id=row["checkpoint_id"],
        codex_workflow_id=row["codex_workflow_id"],
        operation_id=row["operation_id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        remote_status=row["remote_status"],
        changed_files=_load_list(row["changed_files_json"]),
        validation=_load_dict(row["validation_json"]),
        evidence_refs=_load_list(row["evidence_refs_json"]),
        keep=_load_list(row["keep_json"]),
        modify=_load_list(row["modify_json"]),
        drop=_load_list(row["drop_json"]),
        classification_notes=row["classification_notes"],
        classification_status=SnapshotClassificationStatus(row["classification_status"]),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def replan_from_row(row: Any) -> HardReplan:
    return HardReplan(
        replan_id=row["replan_id"],
        task_id=row["task_id"],
        snapshot_id=row["snapshot_id"],
        status=HardReplanStatus(row["status"]),
        from_intent_version=row["from_intent_version"],
        target_intent_version=row["target_intent_version"],
        previous_plan_id=row["previous_plan_id"],
        new_plan_id=row["new_plan_id"],
        new_goal=row["new_goal"],
        reason=row["reason"],
        interrupt_error=row["interrupt_error"],
        new_workflow_id=row["new_workflow_id"],
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def get_work_snapshot(store: MemoryStore, snapshot_id: str) -> WorkSnapshot | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM work_snapshots WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
    return snapshot_from_row(row) if row is not None else None


def latest_work_snapshot(store: MemoryStore, task_id: str) -> WorkSnapshot | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM work_snapshots WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return snapshot_from_row(row) if row is not None else None


def get_hard_replan(store: MemoryStore, replan_id: str) -> HardReplan | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM hard_replans WHERE replan_id = ?",
            (replan_id,),
        ).fetchone()
    return replan_from_row(row) if row is not None else None


def active_hard_replan(store: MemoryStore, task_id: str) -> HardReplan | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            """
            SELECT * FROM hard_replans
            WHERE task_id = ? AND status IN (
                'INTERRUPT_PENDING', 'SNAPSHOT_READY', 'INTERRUPT_FAILED',
                'READY_TO_PLAN', 'PLANNING', 'PLAN_REVIEW'
            )
            ORDER BY created_at DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
    return replan_from_row(row) if row is not None else None


def begin_hard_replan(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    new_goal: str,
    reason: str,
) -> HardReplanBeginResult:
    if not new_goal.strip():
        raise ValueError("new_goal must not be empty")
    if not reason.strip():
        raise ValueError("reason must not be empty")
    now = utcnow()
    now_text = _iso(now)
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        old_task = store._task_from_row(task_row)

        conn.execute(
            """
            UPDATE hard_replans
            SET status = ?, updated_at = ?
            WHERE task_id = ? AND status IN (
                'INTERRUPT_PENDING', 'SNAPSHOT_READY', 'INTERRUPT_FAILED',
                'READY_TO_PLAN', 'PLANNING', 'PLAN_REVIEW'
            )
            """,
            (HardReplanStatus.SUPERSEDED.value, now_text, task_id),
        )

        active_plan = conn.execute(
            """
            SELECT * FROM task_plans
            WHERE task_id = ? AND status IN (?, ?)
            ORDER BY plan_version DESC LIMIT 1
            """,
            (task_id, PlanStatus.APPROVED.value, PlanStatus.DRAFT.value),
        ).fetchone()
        checkpoint = conn.execute(
            "SELECT * FROM codex_checkpoints WHERE task_id = ? ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        runtime = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()

        snapshot = WorkSnapshot(
            snapshot_id=_id("ws"),
            task_id=task_id,
            captured_revision=old_task.revision,
            intent_version=old_task.intent_version,
            plan_version=old_task.plan_version,
            goal=old_task.current_goal,
            phase=old_task.phase.value,
            approved_plan_id=active_plan["plan_id"] if active_plan is not None else None,
            kandev_task_id=old_task.external_kandev_task_id,
            git_branch=old_task.git_branch,
            git_head=old_task.git_head,
            checkpoint_id=checkpoint["checkpoint_id"] if checkpoint is not None else None,
            codex_workflow_id=runtime["workflow_id"] if runtime is not None else None,
            operation_id=runtime["operation_id"] if runtime is not None else None,
            thread_id=runtime["thread_id"] if runtime is not None else old_task.codex_thread_id,
            turn_id=runtime["turn_id"] if runtime is not None else old_task.codex_turn_id,
            remote_status=runtime["remote_status"] if runtime is not None else None,
            changed_files=_load_list(checkpoint["files_changed_json"] if checkpoint is not None else None),
            validation=_load_dict(checkpoint["validation_json"] if checkpoint is not None else None),
            evidence_refs=_load_list(checkpoint["evidence_refs_json"] if checkpoint is not None else None),
            created_at=now,
            updated_at=now,
        )
        conn.execute(
            """
            INSERT INTO work_snapshots(
                snapshot_id, task_id, captured_revision, intent_version, plan_version,
                goal, phase, approved_plan_id, kandev_task_id, git_branch, git_head,
                checkpoint_id, codex_workflow_id, operation_id, thread_id, turn_id,
                remote_status, changed_files_json, validation_json, evidence_refs_json,
                keep_json, modify_json, drop_json, classification_notes,
                classification_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id, snapshot.task_id, snapshot.captured_revision,
                snapshot.intent_version, snapshot.plan_version, snapshot.goal,
                snapshot.phase, snapshot.approved_plan_id, snapshot.kandev_task_id,
                snapshot.git_branch, snapshot.git_head, snapshot.checkpoint_id,
                snapshot.codex_workflow_id, snapshot.operation_id, snapshot.thread_id,
                snapshot.turn_id, snapshot.remote_status, _json(snapshot.changed_files),
                _json(snapshot.validation), _json(snapshot.evidence_refs), "[]", "[]", "[]",
                None, snapshot.classification_status.value, now_text, now_text,
            ),
        )

        if active_plan is not None:
            active_ids = [
                row["plan_id"]
                for row in conn.execute(
                    "SELECT plan_id FROM task_plans WHERE task_id = ? AND status IN (?, ?)",
                    (task_id, PlanStatus.APPROVED.value, PlanStatus.DRAFT.value),
                ).fetchall()
            ]
            conn.execute(
                "UPDATE task_plans SET status = ?, updated_at = ? "
                "WHERE task_id = ? AND status IN (?, ?)",
                (
                    PlanStatus.SUPERSEDED.value,
                    now_text,
                    task_id,
                    PlanStatus.APPROVED.value,
                    PlanStatus.DRAFT.value,
                ),
            )
            for plan_id in active_ids:
                store._sync_document_status(
                    conn,
                    task_id,
                    "plan",
                    plan_id,
                    PlanStatus.SUPERSEDED.value,
                )

        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={
                "current_goal": new_goal.strip(),
                "phase": TaskPhase.PAUSED,
                "current_state": "Hard override recorded; interrupting current Codex work.",
            },
            intent_delta=1,
        )
        replan = HardReplan(
            replan_id=_id("hr"),
            task_id=task_id,
            snapshot_id=snapshot.snapshot_id,
            status=HardReplanStatus.INTERRUPT_PENDING,
            from_intent_version=old_task.intent_version,
            target_intent_version=task.intent_version,
            previous_plan_id=snapshot.approved_plan_id,
            new_goal=new_goal.strip(),
            reason=reason.strip(),
            created_at=now,
            updated_at=now,
        )
        conn.execute(
            """
            INSERT INTO hard_replans(
                replan_id, task_id, snapshot_id, status, from_intent_version,
                target_intent_version, previous_plan_id, new_plan_id, new_goal,
                reason, interrupt_error, new_workflow_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                replan.replan_id, replan.task_id, replan.snapshot_id, replan.status.value,
                replan.from_intent_version, replan.target_intent_version,
                replan.previous_plan_id, None, replan.new_goal, replan.reason,
                None, None, now_text, now_text,
            ),
        )
        if runtime is not None:
            conn.execute(
                "UPDATE codex_runtime_state SET remote_status = ?, next_action = ?, updated_at = ? "
                "WHERE task_id = ?",
                ("interrupt_pending", "interrupt", now_text, task_id),
            )
        store._insert_event(
            conn,
            task,
            Actor.USER,
            EventType.USER_OVERRIDE,
            {
                "kind": "HARD_REPLAN",
                "replan_id": replan.replan_id,
                "snapshot_id": snapshot.snapshot_id,
                "reason": replan.reason,
                "new_goal": replan.new_goal,
            },
        )
        store._insert_event(
            conn,
            task,
            Actor.USER,
            EventType.INTENT_UPDATED,
            {
                "goal": replan.new_goal,
                "intent_version": task.intent_version,
                "replan_id": replan.replan_id,
            },
        )
        if active_plan is not None:
            store._insert_event(
                conn,
                task,
                Actor.SUPERVISOR,
                EventType.PLAN_SUPERSEDED,
                {
                    "reason": "hard_replan",
                    "previous_plan_id": snapshot.approved_plan_id,
                    "replan_id": replan.replan_id,
                },
            )
        return HardReplanBeginResult(task=task, snapshot=snapshot, replan=replan)


def finalize_interrupt(
    store: MemoryStore,
    task_id: str,
    replan_id: str,
    expected_revision: int,
    *,
    succeeded: bool,
    error: str | None = None,
) -> HardReplanBeginResult:
    now = utcnow()
    now_text = _iso(now)
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        replan_row = conn.execute(
            "SELECT * FROM hard_replans WHERE replan_id = ? AND task_id = ?",
            (replan_id, task_id),
        ).fetchone()
        if replan_row is None:
            raise ConflictError(f"Unknown hard replan for task {task_id}: {replan_id}")
        replan = replan_from_row(replan_row)
        if replan.status != HardReplanStatus.INTERRUPT_PENDING:
            raise ConflictError(f"Hard replan is not interrupt-pending: {replan.status.value}")
        status = HardReplanStatus.SNAPSHOT_READY if succeeded else HardReplanStatus.INTERRUPT_FAILED
        phase = TaskPhase.PAUSED if succeeded else TaskPhase.BLOCKED
        state = (
            "Codex interrupted. Classify the work snapshot before replanning."
            if succeeded
            else "Hard replan blocked: current Codex work could not be confirmed interrupted."
        )
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={"phase": phase, "current_state": state},
        )
        conn.execute(
            "UPDATE hard_replans SET status = ?, interrupt_error = ?, updated_at = ? "
            "WHERE replan_id = ?",
            (status.value, error, now_text, replan_id),
        )
        if conn.execute(
            "SELECT 1 FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone():
            conn.execute(
                "UPDATE codex_runtime_state SET remote_status = ?, next_action = ?, updated_at = ? "
                "WHERE task_id = ?",
                (
                    "interrupted" if succeeded else "interrupt_failed",
                    "classify_snapshot" if succeeded else "reconcile_runtime",
                    now_text,
                    task_id,
                ),
            )
        store._insert_event(
            conn,
            task,
            Actor.CODEX if succeeded else Actor.SYSTEM,
            EventType.CODEX_INTERRUPTED if succeeded else EventType.STATE_RECONCILED,
            {
                "replan_id": replan_id,
                "interrupt_succeeded": succeeded,
                "error": error,
            },
        )
        updated_replan = replan_from_row(
            conn.execute("SELECT * FROM hard_replans WHERE replan_id = ?", (replan_id,)).fetchone()
        )
        snapshot = snapshot_from_row(
            conn.execute(
                "SELECT * FROM work_snapshots WHERE snapshot_id = ?",
                (replan.snapshot_id,),
            ).fetchone()
        )
        return HardReplanBeginResult(task=task, snapshot=snapshot, replan=updated_replan)


def classify_work_snapshot(
    store: MemoryStore,
    task_id: str,
    snapshot_id: str,
    expected_revision: int,
    *,
    keep: list[str],
    modify: list[str],
    drop: list[str],
    notes: str | None = None,
) -> HardReplanBeginResult:
    overlaps = (set(keep) & set(modify)) | (set(keep) & set(drop)) | (set(modify) & set(drop))
    if overlaps:
        raise ValueError(f"KEEP/MODIFY/DROP items must be disjoint: {sorted(overlaps)}")
    now = utcnow()
    now_text = _iso(now)
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        replan_row = conn.execute(
            "SELECT * FROM hard_replans WHERE task_id = ? AND snapshot_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (task_id, snapshot_id),
        ).fetchone()
        if replan_row is None:
            raise ConflictError(f"No hard replan owns snapshot {snapshot_id}")
        replan = replan_from_row(replan_row)
        if replan.status != HardReplanStatus.SNAPSHOT_READY:
            raise ConflictError(
                f"Snapshot cannot be classified while replan is {replan.status.value}"
            )
        conn.execute(
            """
            UPDATE work_snapshots
            SET keep_json = ?, modify_json = ?, drop_json = ?, classification_notes = ?,
                classification_status = ?, updated_at = ?
            WHERE snapshot_id = ? AND task_id = ?
            """,
            (
                _json(keep), _json(modify), _json(drop), (notes or "").strip() or None,
                SnapshotClassificationStatus.CLASSIFIED.value, now_text, snapshot_id, task_id,
            ),
        )
        conn.execute(
            "UPDATE hard_replans SET status = ?, updated_at = ? WHERE replan_id = ?",
            (HardReplanStatus.READY_TO_PLAN.value, now_text, replan.replan_id),
        )
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={
                "phase": TaskPhase.REPLANNING,
                "current_state": "Work snapshot classified; ready for a new read-only plan.",
            },
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {
                "kind": "WORK_SNAPSHOT_CLASSIFIED",
                "replan_id": replan.replan_id,
                "snapshot_id": snapshot_id,
                "keep": keep,
                "modify": modify,
                "drop": drop,
            },
        )
        snapshot = snapshot_from_row(
            conn.execute(
                "SELECT * FROM work_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
        )
        updated_replan = replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (replan.replan_id,),
            ).fetchone()
        )
        return HardReplanBeginResult(task=task, snapshot=snapshot, replan=updated_replan)


def bind_replan_workflow(
    store: MemoryStore,
    task_id: str,
    workflow_id: str,
) -> HardReplan | None:
    """Attach derived new-plan workflow identity without creating revision churn."""
    now_text = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            """
            SELECT * FROM hard_replans
            WHERE task_id = ? AND status = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (task_id, HardReplanStatus.READY_TO_PLAN.value),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE hard_replans SET status = ?, new_workflow_id = ?, updated_at = ? "
            "WHERE replan_id = ?",
            (HardReplanStatus.PLANNING.value, workflow_id, now_text, row["replan_id"]),
        )
        return replan_from_row(
            conn.execute(
                "SELECT * FROM hard_replans WHERE replan_id = ?",
                (row["replan_id"],),
            ).fetchone()
        )