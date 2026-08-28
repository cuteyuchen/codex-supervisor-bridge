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
