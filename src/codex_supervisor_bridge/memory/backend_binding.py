from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .errors import ConflictError
from .models import Actor, EventType, TaskMemory, utcnow
from .store import MemoryStore, _dt, _iso


class TaskBackendBinding(BaseModel):
    """Durable task-level backend/profile choice fixed on first workspace bind."""

    task_id: str
    workspace_backend: str
    agent_backend: str
    profile: str
    bound_revision: int = Field(ge=0)
    bound_epoch: int = Field(ge=1)
    updated_at: datetime = Field(default_factory=utcnow)


def _binding_from_row(row: Any) -> TaskBackendBinding:
    return TaskBackendBinding(
        task_id=row["task_id"],
        workspace_backend=row["workspace_backend"],
        agent_backend=row["agent_backend"],
        profile=row["profile"],
        bound_revision=row["bound_revision"],
        bound_epoch=row["bound_epoch"],
        updated_at=_dt(row["updated_at"]),
    )


def get_task_backend_binding(
    store: MemoryStore,
    task_id: str,
) -> TaskBackendBinding | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM task_backend_binding WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _binding_from_row(row) if row is not None else None


def list_active_task_backend_bindings(
    store: MemoryStore,
) -> list[TaskBackendBinding]:
    """Return backend bindings for tasks that still hold a writer lease."""
    with store._lock:
        rows = store._conn.execute(
            """
            SELECT b.*
            FROM task_backend_binding AS b
            JOIN task_execution_state AS e USING (task_id)
            WHERE e.active_writer <> 'NONE'
            ORDER BY b.task_id
            """
        ).fetchall()
    return [_binding_from_row(row) for row in rows]


def bind_task_backend(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    workspace_backend: str,
    agent_backend: str,
    profile: str,
    allow_migration: bool = False,
) -> tuple[TaskMemory, TaskBackendBinding]:
    """Fix the task's backend/profile once the runtime is first composed.

    A later capability drop must not silently switch a bound task to another
    backend. Only an explicit controlled migration may replace the binding.
    """
    if not workspace_backend.strip() or not agent_backend.strip() or not profile.strip():
        raise ValueError("workspace_backend, agent_backend and profile must not be empty")
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        existing = conn.execute(
            "SELECT * FROM task_backend_binding WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing is not None:
            current = _binding_from_row(existing)
            if (
                current.workspace_backend == workspace_backend
                and current.agent_backend == agent_backend
                and current.profile == profile
            ):
                return task_before, current
            if not allow_migration:
                raise ConflictError(
                    "Task is already bound to a different backend; "
                    "an explicit migration is required before switching"
                )
            _assert_migration_safe(conn, task_id)

        now = _iso(utcnow())
        task = store._update_task(conn, task_id, expected_revision)
        conn.execute(
            """
            INSERT INTO task_backend_binding(
                task_id, workspace_backend, agent_backend, profile,
                bound_revision, bound_epoch, updated_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                workspace_backend=excluded.workspace_backend,
                agent_backend=excluded.agent_backend,
                profile=excluded.profile,
                bound_revision=excluded.bound_revision,
                bound_epoch=task_backend_binding.bound_epoch + 1,
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                workspace_backend,
                agent_backend,
                profile,
                task.revision,
                now,
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {
                "kind": "BACKEND_MIGRATED" if existing is not None else "BACKEND_BOUND",
                "workspace_backend": workspace_backend,
                "agent_backend": agent_backend,
                "profile": profile,
            },
        )
        row = conn.execute(
            "SELECT * FROM task_backend_binding WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return task, _binding_from_row(row)


def _assert_migration_safe(conn: Any, task_id: str) -> None:
    """Fail closed when a backend migration would race active work."""
    execution = conn.execute(
        "SELECT active_writer FROM task_execution_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if execution is not None and execution["active_writer"] == "CODEX":
        raise ConflictError(
            "Backend migration is blocked while CODEX is the active writer; "
            "the writer must be quiesced first"
        )

    safety = conn.execute(
        "SELECT state FROM task_agent_safety WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if safety is not None and safety["state"] != "NONE":
        raise ConflictError(
            "Backend migration is blocked by an unresolved agent compensation "
            "or reconciliation latch"
        )

    prepared = conn.execute(
        "SELECT operation_id FROM direct_workspace_operations "
        "WHERE task_id = ? AND status = 'PREPARED' LIMIT 1",
        (task_id,),
    ).fetchone()
    if prepared is not None:
        raise ConflictError(
            "Backend migration is blocked by a pending direct mutation"
        )

    command = conn.execute(
        "SELECT command_id FROM direct_command_sessions "
        "WHERE task_id = ? AND status = 'RUNNING' LIMIT 1",
        (task_id,),
    ).fetchone()
    if command is not None:
        raise ConflictError(
            "Backend migration is blocked by a running direct command session"
        )

    runtime = conn.execute(
        "SELECT remote_status FROM codex_runtime_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if runtime is not None:
        status = (runtime["remote_status"] or "").strip().lower()
        if status in {
            "planning",
            "executing",
            "running",
            "inprogress",
            "in_progress",
            "started",
        }:
            raise ConflictError(
                "Backend migration is blocked while a Codex runtime is active"
            )

    workspace = conn.execute(
        "SELECT state FROM task_workspace_state WHERE task_id = ?",
        (task_id,),
    ).fetchone()
    if workspace is None:
        raise ConflictError(
            "Backend migration requires a saved workspace snapshot before switching"
        )
    if workspace["state"] not in {"ACTIVE", "CLOSED"}:
        raise ConflictError(
            "Backend migration is blocked by a workspace reconciliation state"
        )


def assert_task_backend_binding(
    store: MemoryStore,
    task_id: str,
    *,
    workspace_backend: str,
    agent_backend: str,
) -> TaskBackendBinding | None:
    """Fail closed when a task is bound to a different backend."""
    binding = get_task_backend_binding(store, task_id)
    if binding is None:
        return None
    if (
        binding.workspace_backend != workspace_backend
        or binding.agent_backend != agent_backend
    ):
        raise ConflictError(
            f"Task backend binding is {binding.workspace_backend}/{binding.agent_backend}; "
            f"requested {workspace_backend}/{agent_backend} would silently switch backends"
        )
    return binding
