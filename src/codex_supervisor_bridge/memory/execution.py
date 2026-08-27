from __future__ import annotations

import json
from typing import Any

from .errors import ConflictError
from .execution_models import ExecutionHandoff, ExecutionMutationResult, ExecutionState
from .models import (
    ActiveWriter,
    Actor,
    EventType,
    ExecutionMode,
    HandoffPolicy,
    TaskPhase,
    utcnow,
)
from .store import MemoryStore, _dt, _id, _iso, _json

_TERMINAL_PHASES = {TaskPhase.ACCEPTED, TaskPhase.CANCELLED, TaskPhase.FAILED}


def _state_from_row(row: Any) -> ExecutionState:
    return ExecutionState(
        task_id=row["task_id"],
        execution_mode=ExecutionMode(row["execution_mode"]),
        active_writer=ActiveWriter(row["active_writer"]),
        handoff_policy=HandoffPolicy(row["handoff_policy"]),
        writer_epoch=row["writer_epoch"],
        writer_acquired_revision=row["writer_acquired_revision"],
        updated_at=_dt(row["updated_at"]),
    )


def _handoff_from_row(row: Any) -> ExecutionHandoff:
    return ExecutionHandoff(
        handoff_id=row["handoff_id"],
        task_id=row["task_id"],
        from_writer=ActiveWriter(row["from_writer"]),
        to_writer=ActiveWriter(row["to_writer"]),
        from_revision=row["from_revision"],
        to_revision=row["to_revision"],
        intent_version=row["intent_version"],
        plan_version=row["plan_version"],
        writer_epoch=row["writer_epoch"],
        git_head=row["git_head"],
        change_ref=row["change_ref"],
        validation={} if not row["validation_json"] else json.loads(row["validation_json"]),
        reason=row["reason"],
        actor=row["actor"],
        created_at=_dt(row["created_at"]),
    )


def get_execution_state(store: MemoryStore, task_id: str) -> ExecutionState:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    if row is None:
        raise ConflictError(f"Task execution state is missing: {task_id}")
    return _state_from_row(row)


def list_execution_handoffs(
    store: MemoryStore,
    task_id: str,
    *,
    limit: int = 20,
) -> list[ExecutionHandoff]:
    store.get_task(task_id)
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    with store._lock:
        rows = store._conn.execute(
            "SELECT * FROM execution_handoffs WHERE task_id = ? "
            "ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
    return [_handoff_from_row(row) for row in rows]


def _assert_task_can_write(phase: TaskPhase) -> None:
    if phase in _TERMINAL_PHASES:
        raise ConflictError(f"Task phase does not allow a writer: {phase.value}")


def _assert_workspace_transition_safe(conn: Any, task_id: str) -> None:
    safety = conn.execute(
        "SELECT state FROM task_agent_safety WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if safety is not None and safety["state"] != "NONE":
        raise ConflictError(
            "Agent runtime compensation requires reconciliation before changing execution mode "
            "or writer ownership"
        )
    prepared = conn.execute(
        "SELECT operation_id, operation_type FROM direct_workspace_operations "
        "WHERE task_id = ? AND status = 'PREPARED' LIMIT 1",
        (task_id,),
    ).fetchone()
    if prepared is not None:
        raise ConflictError(
            "Direct workspace operation is still in progress; finish, interrupt, or reconcile it "
            f"before changing writer ownership ({prepared['operation_type']}:{prepared['operation_id']})"
        )
    workspace = conn.execute(
        "SELECT state FROM task_workspace_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if workspace is not None and workspace["state"] == "RECONCILIATION_REQUIRED":
        raise ConflictError(
            "Workspace requires reconciliation before changing execution mode or writer ownership"
        )


def _assert_writer_allowed(
    state: ExecutionState,
    writer: ActiveWriter,
    *,
    actor: Actor,
    explicit_user_authorization: bool,
) -> None:
    if writer == ActiveWriter.NONE:
        raise ValueError("NONE cannot be acquired as a writer")
    if state.execution_mode == ExecutionMode.DIRECT and writer != ActiveWriter.CHATGPT:
        raise ConflictError("DIRECT mode only permits ChatGPT as the workspace writer")
    if state.execution_mode == ExecutionMode.CODEX_SUPERVISED and writer != ActiveWriter.CODEX:
        raise ConflictError("CODEX_SUPERVISED mode only permits Codex as the workspace writer")
    if (
        state.execution_mode == ExecutionMode.HYBRID
        and writer == ActiveWriter.CODEX
        and state.handoff_policy == HandoffPolicy.MANUAL_ONLY
        and actor != Actor.USER
        and not explicit_user_authorization
    ):
        raise ConflictError(
            "HYBRID task requires explicit user authorization before Supervisor delegates to Codex"
        )


def set_execution_mode(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    mode: ExecutionMode,
    *,
    actor: Actor = Actor.USER,
) -> ExecutionMutationResult:
    now = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        state_row = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if state_row is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        state_before = _state_from_row(state_row)
        if state_before.execution_mode == mode:
            return ExecutionMutationResult(task=task_before, execution=state_before)
        _assert_workspace_transition_safe(conn, task_id)
        if state_before.active_writer == ActiveWriter.CHATGPT and mode == ExecutionMode.CODEX_SUPERVISED:
            raise ConflictError("Release or hand off the ChatGPT writer before switching to CODEX_SUPERVISED")
        if state_before.active_writer == ActiveWriter.CODEX and mode == ExecutionMode.DIRECT:
            raise ConflictError("Interrupt/release or hand back the Codex writer before switching to DIRECT")
        task = store._update_task(conn, task_id, expected_revision)
        conn.execute(
            "UPDATE task_execution_state SET execution_mode = ?, updated_at = ? WHERE task_id = ?",
            (mode.value, now, task_id),
        )
        store._insert_event(
            conn,
            task,
            actor,
            EventType.EXECUTION_MODE_CHANGED,
            {
                "from": state_before.execution_mode.value,
                "to": mode.value,
                "active_writer": state_before.active_writer.value,
            },
        )
        state = _state_from_row(
            conn.execute(
                "SELECT * FROM task_execution_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
        return ExecutionMutationResult(task=task, execution=state)


def set_handoff_policy(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    policy: HandoffPolicy,
    *,
    actor: Actor = Actor.USER,
) -> ExecutionMutationResult:
    if policy == HandoffPolicy.SUPERVISOR_ALLOWED and actor != Actor.USER:
        raise ConflictError("Only an explicit user action may enable automatic Supervisor delegation")
    now = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        state_row = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if state_row is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        state_before = _state_from_row(state_row)
        if state_before.handoff_policy == policy:
            return ExecutionMutationResult(task=task_before, execution=state_before)
        task = store._update_task(conn, task_id, expected_revision)
        conn.execute(
            "UPDATE task_execution_state SET handoff_policy = ?, updated_at = ? WHERE task_id = ?",
            (policy.value, now, task_id),
        )
        store._insert_event(
            conn,
            task,
            actor,
            EventType.STATE_RECONCILED,
            {
                "kind": "HANDOFF_POLICY_CHANGED",
                "from": state_before.handoff_policy.value,
                "to": policy.value,
            },
        )
        state = _state_from_row(
            conn.execute(
                "SELECT * FROM task_execution_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
        return ExecutionMutationResult(task=task, execution=state)


def acquire_writer(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    writer: ActiveWriter,
    *,
    actor: Actor = Actor.SUPERVISOR,
    explicit_user_authorization: bool = False,
) -> ExecutionMutationResult:
    now = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        _assert_task_can_write(task_before.phase)
        _assert_workspace_transition_safe(conn, task_id)
        state_row = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if state_row is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        state_before = _state_from_row(state_row)
        if state_before.active_writer != ActiveWriter.NONE:
            raise ConflictError(
                f"Workspace writer is already owned by {state_before.active_writer.value} "
                f"at epoch {state_before.writer_epoch}"
            )
        _assert_writer_allowed(
            state_before,
            writer,
            actor=actor,
            explicit_user_authorization=explicit_user_authorization,
        )
        task = store._update_task(conn, task_id, expected_revision)
        epoch = state_before.writer_epoch + 1
        conn.execute(
            """
            UPDATE task_execution_state
            SET active_writer = ?, writer_epoch = ?, writer_acquired_revision = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (writer.value, epoch, task.revision, now, task_id),
        )
        store._insert_event(
            conn,
            task,
            actor,
            EventType.WRITER_ACQUIRED,
            {"writer": writer.value, "writer_epoch": epoch},
        )
        state = _state_from_row(
            conn.execute(
                "SELECT * FROM task_execution_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
        return ExecutionMutationResult(task=task, execution=state)


def release_writer(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    writer: ActiveWriter,
    expected_writer_epoch: int,
    *,
    actor: Actor = Actor.SUPERVISOR,
) -> ExecutionMutationResult:
    now = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        state_row = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if state_row is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        state_before = _state_from_row(state_row)
        if state_before.active_writer != writer:
            raise ConflictError(
                f"Writer release mismatch: current={state_before.active_writer.value}, requested={writer.value}"
            )
        if state_before.writer_epoch != expected_writer_epoch:
            raise ConflictError(
                f"Writer epoch mismatch: expected={expected_writer_epoch}, current={state_before.writer_epoch}"
            )
        _assert_workspace_transition_safe(conn, task_id)
        task = store._update_task(conn, task_id, expected_revision)
        conn.execute(
            """
            UPDATE task_execution_state
            SET active_writer = 'NONE', writer_acquired_revision = NULL, updated_at = ?
            WHERE task_id = ?
            """,
            (now, task_id),
        )
        store._insert_event(
            conn,
            task,
            actor,
            EventType.WRITER_RELEASED,
            {"writer": writer.value, "writer_epoch": state_before.writer_epoch},
        )
        state = _state_from_row(
            conn.execute(
                "SELECT * FROM task_execution_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
        return ExecutionMutationResult(task=task, execution=state)


def handoff_writer(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    from_writer: ActiveWriter,
    to_writer: ActiveWriter,
    expected_writer_epoch: int,
    reason: str,
    git_head: str | None = None,
    change_ref: str | None = None,
    validation: dict[str, Any] | None = None,
    actor: Actor = Actor.SUPERVISOR,
    explicit_user_authorization: bool = False,
) -> ExecutionMutationResult:
    if from_writer == ActiveWriter.NONE or to_writer == ActiveWriter.NONE:
        raise ValueError("Atomic handoff requires two concrete writers")
    if from_writer == to_writer:
        raise ValueError("Handoff writers must differ")
    if not reason.strip():
        raise ValueError("handoff reason must not be empty")
    now = utcnow()
    now_text = _iso(now)
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        _assert_task_can_write(task_before.phase)
        state_row = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if state_row is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        state_before = _state_from_row(state_row)
        if state_before.active_writer != from_writer:
            raise ConflictError(
                f"Handoff source mismatch: current={state_before.active_writer.value}, "
                f"requested={from_writer.value}"
            )
        if state_before.writer_epoch != expected_writer_epoch:
            raise ConflictError(
                f"Writer epoch mismatch: expected={expected_writer_epoch}, current={state_before.writer_epoch}"
            )
        _assert_workspace_transition_safe(conn, task_id)
        _assert_writer_allowed(
            state_before,
            to_writer,
            actor=actor,
            explicit_user_authorization=explicit_user_authorization,
        )
        task = store._update_task(conn, task_id, expected_revision)
        epoch = state_before.writer_epoch + 1
        conn.execute(
            """
            UPDATE task_execution_state
            SET active_writer = ?, writer_epoch = ?, writer_acquired_revision = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (to_writer.value, epoch, task.revision, now_text, task_id),
        )
        handoff = ExecutionHandoff(
            handoff_id=_id("handoff"),
            task_id=task_id,
            from_writer=from_writer,
            to_writer=to_writer,
            from_revision=expected_revision,
            to_revision=task.revision,
            intent_version=task.intent_version,
            plan_version=task.plan_version,
            writer_epoch=epoch,
            git_head=git_head or task.git_head,
            change_ref=change_ref,
            validation=validation or {},
            reason=reason.strip(),
            actor=actor.value,
            created_at=now,
        )
        conn.execute(
            """
            INSERT INTO execution_handoffs(
                handoff_id, task_id, from_writer, to_writer, from_revision, to_revision,
                intent_version, plan_version, writer_epoch, git_head, change_ref,
                validation_json, reason, actor, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                handoff.handoff_id,
                handoff.task_id,
                handoff.from_writer.value,
                handoff.to_writer.value,
                handoff.from_revision,
                handoff.to_revision,
                handoff.intent_version,
                handoff.plan_version,
                handoff.writer_epoch,
                handoff.git_head,
                handoff.change_ref,
                _json(handoff.validation),
                handoff.reason,
                handoff.actor,
                _iso(handoff.created_at),
            ),
        )
        event_type = (
            EventType.EXECUTION_HANDBACK
            if to_writer == ActiveWriter.CHATGPT
            else EventType.EXECUTION_HANDOFF
        )
        store._insert_event(
            conn,
            task,
            actor,
            event_type,
            {
                "handoff_id": handoff.handoff_id,
                "from_writer": from_writer.value,
                "to_writer": to_writer.value,
                "writer_epoch": epoch,
                "git_head": handoff.git_head,
                "change_ref": handoff.change_ref,
                "reason": handoff.reason,
            },
        )
        state = _state_from_row(
            conn.execute(
                "SELECT * FROM task_execution_state WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
        return ExecutionMutationResult(task=task, execution=state, handoff=handoff)
