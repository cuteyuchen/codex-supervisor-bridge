from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from .models import Actor, EventType, TaskMemory, TaskPhase, utcnow
from .store import MemoryStore, _dt, _iso


class CodexRuntimeState(BaseModel):
    task_id: str
    workflow_id: str | None = None
    operation_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    remote_status: str | None = None
    next_action: str | None = None
    last_client_request_id: str | None = None
    updated_at: datetime


def _state_from_row(row: Any) -> CodexRuntimeState:
    return CodexRuntimeState(
        task_id=row["task_id"],
        workflow_id=row["workflow_id"],
        operation_id=row["operation_id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        remote_status=row["remote_status"],
        next_action=row["next_action"],
        last_client_request_id=row["last_client_request_id"],
        updated_at=_dt(row["updated_at"]),
    )


def get_codex_runtime(store: MemoryStore, task_id: str) -> CodexRuntimeState | None:
    """Read the latest durable Codex control-plane identity without changing revision."""
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _state_from_row(row) if row is not None else None


def bind_codex_runtime(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    event_type: EventType,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    remote_status: str | None = None,
    next_action: str | None = None,
    client_request_id: str | None = None,
    task_phase: TaskPhase | None = None,
    current_state: str | None = None,
    event_payload: dict[str, Any] | None = None,
) -> tuple[TaskMemory, CodexRuntimeState]:
    """Persist a supervisor-authorized Codex control transition atomically.

    This is intentionally revision protected. Read-only polling uses
    ``get_codex_runtime`` and does not mutate task state. Control actions update
    the task phase/thread/turn snapshot and the dedicated durable control-plane
    identity row in one revision transaction, then append an auditable event.
    """
    if event_type not in {
        EventType.CODEX_STARTED,
        EventType.CODEX_PROGRESS,
        EventType.CODEX_STEERED,
        EventType.CODEX_INTERRUPTED,
        EventType.CODEX_COMPLETED,
    }:
        raise ValueError(f"Unsupported Codex runtime event: {event_type.value}")

    with store._write() as conn:
        row = store._task_row(conn, task_id)
        store._assert_revision(row, expected_revision)
        existing = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()

        def choose(column: str, supplied: str | None) -> str | None:
            if supplied is not None:
                return supplied
            return existing[column] if existing is not None else None

        effective_thread = choose("thread_id", thread_id)
        effective_turn = choose("turn_id", turn_id)
        values: dict[str, Any] = {}
        if effective_thread is not None:
            values["codex_thread_id"] = effective_thread
        if effective_turn is not None:
            values["codex_turn_id"] = effective_turn
        if task_phase is not None:
            values["phase"] = task_phase
        if current_state is not None:
            values["current_state"] = current_state
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values=values,
        )

        now = _iso(utcnow())
        state = CodexRuntimeState(
            task_id=task_id,
            workflow_id=choose("workflow_id", workflow_id),
            operation_id=choose("operation_id", operation_id),
            thread_id=effective_thread,
            turn_id=effective_turn,
            remote_status=choose("remote_status", remote_status),
            next_action=choose("next_action", next_action),
            last_client_request_id=choose("last_client_request_id", client_request_id),
            updated_at=_dt(now),
        )
        conn.execute(
            """
            INSERT INTO codex_runtime_state(
                task_id, workflow_id, operation_id, thread_id, turn_id,
                remote_status, next_action, last_client_request_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                workflow_id=excluded.workflow_id,
                operation_id=excluded.operation_id,
                thread_id=excluded.thread_id,
                turn_id=excluded.turn_id,
                remote_status=excluded.remote_status,
                next_action=excluded.next_action,
                last_client_request_id=excluded.last_client_request_id,
                updated_at=excluded.updated_at
            """,
            (
                state.task_id,
                state.workflow_id,
                state.operation_id,
                state.thread_id,
                state.turn_id,
                state.remote_status,
                state.next_action,
                state.last_client_request_id,
                now,
            ),
        )
        payload = {
            "workflow_id": state.workflow_id,
            "operation_id": state.operation_id,
            "thread_id": state.thread_id,
            "turn_id": state.turn_id,
            "remote_status": state.remote_status,
            "next_action": state.next_action,
            "client_request_id": state.last_client_request_id,
            "task_phase": task.phase.value,
        }
        payload.update(event_payload or {})
        store._insert_event(conn, task, Actor.CODEX, event_type, payload)
        return task, state
