from __future__ import annotations

import json
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .errors import ConflictError
from .models import Actor, EventType, utcnow
from .store import MemoryStore, _dt, _iso, _json


class AgentSafetyState(str, Enum):
    NONE = "NONE"
    COMPENSATION_REQUIRED = "COMPENSATION_REQUIRED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class AgentSafety(BaseModel):
    task_id: str
    state: str = AgentSafetyState.NONE
    operation: str
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)
    workflow_id: str | None = None
    operation_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    updated_at: datetime


def _from_row(row: Any) -> AgentSafety:
    return AgentSafety(
        task_id=row["task_id"],
        state=row["state"],
        operation=row["operation"],
        summary=row["summary"],
        details=json.loads(row["details_json"] or "{}"),
        workflow_id=row["workflow_id"],
        operation_id=row["operation_id"],
        thread_id=row["thread_id"],
        turn_id=row["turn_id"],
        updated_at=_dt(row["updated_at"]),
    )


def get_agent_safety(store: MemoryStore, task_id: str) -> AgentSafety | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM task_agent_safety WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _from_row(row) if row is not None else None


def assert_agent_safety_clear(store: MemoryStore, task_id: str) -> None:
    safety = get_agent_safety(store, task_id)
    if safety is not None and safety.state != AgentSafetyState.NONE:
        raise ConflictError(
            "Agent runtime compensation requires reconciliation before changing writer ownership "
            "or starting another workspace write"
        )


def _record(
    store: MemoryStore,
    task_id: str,
    *,
    state: str,
    operation: str,
    summary: str,
    details: dict[str, Any] | None,
    workflow_id: str | None,
    operation_id: str | None,
    thread_id: str | None,
    turn_id: str | None,
    event_type: EventType,
) -> AgentSafety:
    now = _iso(utcnow())
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        current = store._task_from_row(task_row)
        task = store._update_task(
            conn,
            task_id,
            current.revision,
        )
        conn.execute(
            """
            INSERT INTO task_agent_safety(
                task_id, state, operation, summary, details_json,
                workflow_id, operation_id, thread_id, turn_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                state=excluded.state,
                operation=excluded.operation,
                summary=excluded.summary,
                details_json=excluded.details_json,
                workflow_id=excluded.workflow_id,
                operation_id=excluded.operation_id,
                thread_id=excluded.thread_id,
                turn_id=excluded.turn_id,
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                state,
                operation,
                summary,
                _json(details or {}),
                workflow_id,
                operation_id,
                thread_id,
                turn_id,
                now,
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            event_type,
            {
                "operation": operation,
                "state": state,
                "summary": summary,
                "workflow_id": workflow_id,
                "operation_id": operation_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                **(details or {}),
            },
        )
        return _from_row(
            conn.execute(
                "SELECT * FROM task_agent_safety WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )


def record_agent_compensation_succeeded(
    store: MemoryStore,
    task_id: str,
    *,
    operation: str,
    summary: str,
    details: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> AgentSafety:
    return _record(
        store,
        task_id,
        state=AgentSafetyState.NONE,
        operation=operation,
        summary=summary,
        details=details,
        workflow_id=workflow_id,
        operation_id=operation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        event_type=EventType.AGENT_COMPENSATION_SUCCEEDED,
    )


def latch_agent_compensation(
    store: MemoryStore,
    task_id: str,
    *,
    operation: str,
    summary: str,
    details: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
) -> AgentSafety:
    """Durably fence all new writes before waiting on remote interruption."""
    return _record(
        store,
        task_id,
        state=AgentSafetyState.COMPENSATION_REQUIRED,
        operation=operation,
        summary=summary,
        details=details,
        workflow_id=workflow_id,
        operation_id=operation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        event_type=EventType.AGENT_COMPENSATION_REQUIRED,
    )


def record_agent_compensation_required(
    store: MemoryStore,
    task_id: str,
    *,
    operation: str,
    summary: str,
    details: dict[str, Any] | None = None,
    workflow_id: str | None = None,
    operation_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    reconciliation_required: bool = True,
) -> AgentSafety:
    return _record(
        store,
        task_id,
        state=(
            AgentSafetyState.RECONCILIATION_REQUIRED
            if reconciliation_required
            else AgentSafetyState.COMPENSATION_REQUIRED
        ),
        operation=operation,
        summary=summary,
        details=details,
        workflow_id=workflow_id,
        operation_id=operation_id,
        thread_id=thread_id,
        turn_id=turn_id,
        event_type=EventType.AGENT_COMPENSATION_REQUIRED,
    )


def reconcile_agent_safety(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    summary: str = "Agent runtime compensation was reconciled.",
) -> AgentSafety:
    now = _iso(utcnow())
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={"current_state": summary},
        )
        row = conn.execute(
            "SELECT * FROM task_agent_safety WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ConflictError(f"No agent safety state exists for task {task_id}")
        conn.execute(
            "UPDATE task_agent_safety SET state = 'NONE', summary = ?, updated_at = ? "
            "WHERE task_id = ?",
            (summary, now, task_id),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {"kind": "AGENT_COMPENSATION_RECONCILED", "summary": summary},
        )
        return _from_row(
            conn.execute(
                "SELECT * FROM task_agent_safety WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        )
