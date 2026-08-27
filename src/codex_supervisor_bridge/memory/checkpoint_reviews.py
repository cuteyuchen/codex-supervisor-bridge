from __future__ import annotations

from typing import Any

from .checkpoint_models import CheckpointReview, CheckpointReviewDecision
from .checkpoint_store import checkpoint_from_row
from .errors import ConflictError
from .models import Actor, EventType, TaskMemory, TaskPhase
from .store import MemoryStore, _dt, _id, _iso


def review_from_row(row: Any) -> CheckpointReview:
    return CheckpointReview(
        review_id=row["review_id"],
        checkpoint_id=row["checkpoint_id"],
        task_id=row["task_id"],
        decision=CheckpointReviewDecision(row["decision"]),
        instruction=row["instruction"],
        reviewed_revision=row["reviewed_revision"],
        created_at=_dt(row["created_at"]),
    )


def get_checkpoint_review(store: MemoryStore, checkpoint_id: str) -> CheckpointReview | None:
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM checkpoint_reviews WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone()
    return review_from_row(row) if row is not None else None


def review_checkpoint(
    store: MemoryStore,
    task_id: str,
    checkpoint_id: str,
    expected_revision: int,
    decision: CheckpointReviewDecision,
    *,
    instruction: str | None = None,
) -> tuple[TaskMemory, CheckpointReview]:
    if decision == CheckpointReviewDecision.STEER and not (instruction or "").strip():
        raise ValueError("STEER review requires a concrete instruction")
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        cp_row = conn.execute(
            "SELECT * FROM codex_checkpoints WHERE checkpoint_id = ? AND task_id = ?",
            (checkpoint_id, task_id),
        ).fetchone()
        if cp_row is None:
            raise ConflictError(f"Unknown checkpoint for task {task_id}: {checkpoint_id}")
        checkpoint = checkpoint_from_row(cp_row)
        if not checkpoint.requires_review:
            raise ConflictError("Heartbeat checkpoints do not require Supervisor review")
        if conn.execute(
            "SELECT 1 FROM checkpoint_reviews WHERE checkpoint_id = ?",
            (checkpoint_id,),
        ).fetchone():
            raise ConflictError(f"Checkpoint already reviewed: {checkpoint_id}")
        newest = conn.execute(
            """
            SELECT c.checkpoint_id
            FROM codex_checkpoints c
            LEFT JOIN checkpoint_reviews r ON r.checkpoint_id = c.checkpoint_id
            WHERE c.task_id = ? AND c.requires_review = 1 AND r.review_id IS NULL
            ORDER BY c.sequence DESC LIMIT 1
            """,
            (task_id,),
        ).fetchone()
        if newest is None or newest["checkpoint_id"] != checkpoint_id:
            raise ConflictError("Read and review the latest unreviewed checkpoint")
        phase = TaskPhase.IMPLEMENTING
        if decision in {CheckpointReviewDecision.INTERRUPT, CheckpointReviewDecision.REPLAN}:
            phase = TaskPhase.SUPERVISOR_REVIEW
        elif decision == CheckpointReviewDecision.ACCEPT:
            phase = TaskPhase.FINAL_REVIEW
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={"phase": phase},
        )
        review = CheckpointReview(
            review_id=_id("cpr"),
            checkpoint_id=checkpoint_id,
            task_id=task_id,
            decision=decision,
            instruction=(instruction or "").strip() or None,
            reviewed_revision=task.revision,
        )
        conn.execute(
            """
            INSERT INTO checkpoint_reviews(
                review_id, checkpoint_id, task_id, decision, instruction,
                reviewed_revision, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                review.review_id, review.checkpoint_id, review.task_id,
                review.decision.value, review.instruction, review.reviewed_revision,
                _iso(review.created_at),
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.CHECKPOINT_REVIEWED,
            {
                "checkpoint_id": checkpoint_id,
                "decision": decision.value,
                "instruction": review.instruction,
            },
        )
        return task, review


def recommended_next_tool(decision: CheckpointReviewDecision) -> str | None:
    return {
        CheckpointReviewDecision.CONTINUE: None,
        CheckpointReviewDecision.STEER: "soft_steer_codex",
        CheckpointReviewDecision.INTERRUPT: "interrupt_codex",
        CheckpointReviewDecision.REPLAN: "interrupt_codex",
        CheckpointReviewDecision.ACCEPT: None,
    }[decision]
