from __future__ import annotations

from .errors import ConflictError
from .models import Actor, EventType, TaskMemory
from .store import MemoryStore


def bind_kandev_task(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    kandev_task_id: str,
    *,
    external_id: str,
) -> TaskMemory:
    """Bind a supervisor task to exactly one Kandev task.

    The operation uses the same optimistic revision transaction as the rest of
    the memory core. Rebinding to a different Kandev task is rejected rather
    than silently changing external identity. Replaying the same binding at
    the current revision is an idempotent no-op.
    """
    if not kandev_task_id:
        raise ValueError("kandev_task_id must not be empty")

    with store._write() as conn:
        row = store._task_row(conn, task_id)
        store._assert_revision(row, expected_revision)
        existing = row["external_kandev_task_id"]
        if existing:
            if existing != kandev_task_id:
                raise ConflictError(
                    f"Task {task_id} is already bound to Kandev task {existing}; "
                    f"refusing rebind to {kandev_task_id}"
                )
            return store._task_from_row(row)

        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={"external_kandev_task_id": kandev_task_id},
        )
        store._insert_event(
            conn,
            task,
            Actor.KANDEV,
            EventType.KANDEV_TASK_BOUND,
            {
                "kandev_task_id": kandev_task_id,
                "external_id": external_id,
            },
        )
        return task
