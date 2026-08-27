from __future__ import annotations

from pathlib import Path

from .context_pack import BuiltContextPack, ContextPackBuilder
from .errors import StaleRevisionError
from .models import (
    ConstraintSeverity,
    ContextPackMode,
    MemorySearchHit,
    TaskMemory,
)
from .store import MemoryStore


class MemoryService:
    """Application-facing facade for the P1 persistent memory core."""

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        target_context_chars: int = 48_000,
        hard_max_context_chars: int = 64_000,
    ) -> None:
        self.store = MemoryStore(database)
        self.context_builder = ContextPackBuilder(
            self.store,
            target_chars=target_context_chars,
            hard_max_chars=hard_max_context_chars,
        )

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "MemoryService":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def create_task(
        self,
        task_id: str,
        title: str,
        *,
        repository: str | None = None,
        goal: str | None = None,
        hard_constraints: list[str] | None = None,
    ) -> TaskMemory:
        task = self.store.create_task(task_id, title, repository=repository, goal=goal)
        for constraint in hard_constraints or []:
            self.store.add_constraint(
                task_id,
                task.revision,
                constraint,
                severity=ConstraintSeverity.HARD,
            )
            task = self.store.get_task(task_id)
        return task

    def resume_task(
        self,
        task_id: str,
        *,
        mode: ContextPackMode = ContextPackMode.RESUME,
        persist_snapshot: bool = True,
    ) -> BuiltContextPack:
        return self.context_builder.build(
            task_id,
            mode=mode,
            persist_snapshot=persist_snapshot,
        )

    def get_context_pack(
        self,
        task_id: str,
        *,
        mode: ContextPackMode = ContextPackMode.RESUME,
    ) -> BuiltContextPack:
        return self.context_builder.build(task_id, mode=mode, persist_snapshot=False)

    def search_task_memory(
        self,
        task_id: str,
        query: str,
        *,
        limit: int = 10,
    ) -> list[MemorySearchHit]:
        return self.store.search(task_id, query, limit=limit)

    def assert_revision(self, task_id: str, expected_revision: int) -> TaskMemory:
        task = self.store.get_task(task_id)
        if task.revision != expected_revision:
            raise StaleRevisionError(task_id, expected_revision, task.revision)
        return task
