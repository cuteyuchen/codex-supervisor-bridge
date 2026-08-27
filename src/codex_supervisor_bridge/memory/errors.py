from __future__ import annotations


class MemoryErrorBase(RuntimeError):
    """Base error for persistent task memory operations."""


class TaskNotFoundError(MemoryErrorBase):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Unknown supervised task: {task_id}")
        self.task_id = task_id


class StaleRevisionError(MemoryErrorBase):
    def __init__(self, task_id: str, expected_revision: int, current_revision: int) -> None:
        super().__init__(
            f"STALE_CONTEXT task={task_id} expected_revision={expected_revision} "
            f"current_revision={current_revision}"
        )
        self.task_id = task_id
        self.expected_revision = expected_revision
        self.current_revision = current_revision


class ConflictError(MemoryErrorBase):
    """Raised when a memory mutation conflicts with existing durable state."""


class InvalidTransitionError(MemoryErrorBase):
    """Raised when a task/plan/decision transition is not valid from its current state."""
