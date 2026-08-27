from __future__ import annotations

import json
from typing import Any

from .checkpoint_models import CheckpointCreateResult, CheckpointType, CodexCheckpoint
from .models import Actor, EventType, TaskPhase
from .store import MemoryStore, _dt, _id, _iso, _json


def _list(value: str) -> list[str]:
    decoded = json.loads(value)
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _dict(value: str) -> dict[str, Any]:
    decoded = json.loads(value)
    return decoded if isinstance(decoded, dict) else {}


def checkpoint_from_row(row: Any) -> CodexCheckpoint:
    return CodexCheckpoint(
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        sequence=row["sequence"],
        checkpoint_type=CheckpointType(row["checkpoint_type"]),
        task_revision=row["task_revision"],
        intent_version=row["intent_version"],
        plan_version=row["plan_version"],
        workflow_id=row["workflow_id"],
        operation_id=row["operation_id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        remote_status=row["remote_status"],
        next_action=row["next_action"],
        trigger_reason=row["trigger_reason"],
        completed=_list(row["completed_json"]),
        in_progress=_list(row["in_progress_json"]),
        files_changed=_list(row["files_changed_json"]),
        validation=_dict(row["validation_json"]),
        assumptions=_list(row["assumptions_json"]),
        deviations=_list(row["deviations_json"]),
        blockers=_list(row["blockers_json"]),
        risks=_list(row["risks_json"]),
        next_steps=_list(row["next_steps_json"]),
        evidence_refs=_list(row["evidence_refs_json"]),
        source_fingerprint=row["source_fingerprint"],
        raw_event_count=row["raw_event_count"],
        requires_review=bool(row["requires_review"]),
        created_at=_dt(row["created_at"]),
    )


def get_checkpoint(store: MemoryStore, checkpoint_id: str) -> CodexCheckpoint | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM codex_checkpoints WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
    return checkpoint_from_row(row) if row is not None else None


def latest_checkpoint(store: MemoryStore, task_id: str) -> CodexCheckpoint | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM codex_checkpoints WHERE task_id = ? ORDER BY sequence DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return checkpoint_from_row(row) if row is not None else None


def list_checkpoints(store: MemoryStore, task_id: str, *, limit: int = 20) -> list[CodexCheckpoint]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    store.get_task(task_id)
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM codex_checkpoints WHERE task_id = ? ORDER BY sequence DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    return [checkpoint_from_row(row) for row in rows]


def create_checkpoint(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    checkpoint_type: CheckpointType,
    source_fingerprint: str,
    trigger_reason: str,
    runtime: dict[str, str | None] | None = None,
    completed: list[str] | None = None,
    in_progress: list[str] | None = None,
    files_changed: list[str] | None = None,
    validation: dict[str, Any] | None = None,
    assumptions: list[str] | None = None,
    deviations: list[str] | None = None,
    blockers: list[str] | None = None,
    risks: list[str] | None = None,
    next_steps: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    raw_event_count: int = 0,
) -> CheckpointCreateResult:
    runtime = runtime or {}
    with store._write() as conn:
        row = store._task_row(conn, task_id)
        store._assert_revision(row, expected_revision)
        duplicate = conn.execute(
            "SELECT * FROM codex_checkpoints WHERE task_id = ? AND source_fingerprint = ?",
            (task_id, source_fingerprint),
        ).fetchone()
        if duplicate is not None:
            return CheckpointCreateResult(
                checkpoint=checkpoint_from_row(duplicate),
                created=False,
                task=store._task_from_row(row),
            )
        seq_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS n FROM codex_checkpoints WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        requires_review = checkpoint_type != CheckpointType.HEARTBEAT
        task = (
            store._update_task(
                conn,
                task_id,
                expected_revision,
                values={"phase": TaskPhase.SUPERVISOR_REVIEW},
            )
            if requires_review
            else store._task_from_row(row)
        )
        checkpoint = CodexCheckpoint(
            checkpoint_id=_id("cp"),
            task_id=task_id,
            sequence=int(seq_row["n"]),
            checkpoint_type=checkpoint_type,
            task_revision=task.revision,
            intent_version=task.intent_version,
            plan_version=task.plan_version,
            workflow_id=runtime.get("workflow_id"),
            operation_id=runtime.get("operation_id"),
            thread_id=runtime.get("thread_id"),
            turn_id=runtime.get("turn_id"),
            remote_status=runtime.get("remote_status"),
            next_action=runtime.get("next_action"),
            trigger_reason=trigger_reason.strip(),
            completed=completed or [],
            in_progress=in_progress or [],
            files_changed=files_changed or [],
            validation=validation or {},
            assumptions=assumptions or [],
            deviations=deviations or [],
            blockers=blockers or [],
            risks=risks or [],
            next_steps=next_steps or [],
            evidence_refs=evidence_refs or [],
            source_fingerprint=source_fingerprint,
            raw_event_count=raw_event_count,
            requires_review=requires_review,
        )
        conn.execute(
            """
            INSERT INTO codex_checkpoints(
                checkpoint_id, task_id, sequence, checkpoint_type, task_revision,
                intent_version, plan_version, workflow_id, operation_id, thread_id,
                turn_id, remote_status, next_action, trigger_reason, completed_json,
                in_progress_json, files_changed_json, validation_json, assumptions_json,
                deviations_json, blockers_json, risks_json, next_steps_json,
                evidence_refs_json, source_fingerprint, raw_event_count,
                requires_review, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                checkpoint.checkpoint_id, checkpoint.task_id, checkpoint.sequence,
                checkpoint.checkpoint_type.value, checkpoint.task_revision,
                checkpoint.intent_version, checkpoint.plan_version, checkpoint.workflow_id,
                checkpoint.operation_id, checkpoint.thread_id, checkpoint.turn_id,
                checkpoint.remote_status, checkpoint.next_action, checkpoint.trigger_reason,
                _json(checkpoint.completed), _json(checkpoint.in_progress),
                _json(checkpoint.files_changed), _json(checkpoint.validation),
                _json(checkpoint.assumptions), _json(checkpoint.deviations),
                _json(checkpoint.blockers), _json(checkpoint.risks),
                _json(checkpoint.next_steps), _json(checkpoint.evidence_refs),
                checkpoint.source_fingerprint, checkpoint.raw_event_count,
                int(checkpoint.requires_review), _iso(checkpoint.created_at),
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.CODEX,
            EventType.CHECKPOINT_CREATED,
            {
                "checkpoint_id": checkpoint.checkpoint_id,
                "sequence": checkpoint.sequence,
                "checkpoint_type": checkpoint.checkpoint_type.value,
                "requires_review": checkpoint.requires_review,
                "trigger_reason": checkpoint.trigger_reason,
            },
        )
        return CheckpointCreateResult(checkpoint=checkpoint, created=True, task=task)
