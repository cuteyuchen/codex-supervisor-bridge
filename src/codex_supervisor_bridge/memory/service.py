from __future__ import annotations

from pathlib import Path

from .context_pack import BuiltContextPack, ContextPackBuilder
from .errors import StaleRevisionError
from .kandev_binding import bind_kandev_task
from .models import (
    Actor,
    Constraint,
    ConstraintSeverity,
    ContextPackMode,
    Decision,
    EventType,
    MemorySearchHit,
    Plan,
    TaskEvent,
    TaskMemory,
)
from .store import MemoryStore


class MemoryService:
    """Application-facing facade for persistent supervisor memory."""

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

    def get_task(self, task_id: str) -> TaskMemory:
        return self.store.get_task(task_id)

    def timeline(self, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
        return self.store.list_events(task_id, limit=limit)

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

    def record_user_override(
        self,
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> TaskEvent:
        return self.store.record_event(
            task_id,
            expected_revision,
            Actor.USER,
            EventType.USER_OVERRIDE,
            {"instruction": instruction},
        )

    def update_intent(
        self,
        task_id: str,
        expected_revision: int,
        goal: str,
    ) -> TaskMemory:
        return self.store.update_intent(task_id, expected_revision, goal, actor=Actor.USER)

    def add_decision(
        self,
        task_id: str,
        expected_revision: int,
        title: str,
        content: str,
        *,
        decision_type: str = "general",
    ) -> Decision:
        return self.store.add_decision(
            task_id,
            expected_revision,
            title,
            content,
            decision_type=decision_type,
            actor=Actor.SUPERVISOR,
        )

    def supersede_decision(
        self,
        task_id: str,
        expected_revision: int,
        decision_id: str,
        *,
        superseded_by: str | None = None,
    ) -> Decision:
        return self.store.supersede_decision(
            task_id,
            expected_revision,
            decision_id,
            superseded_by=superseded_by,
            actor=Actor.SUPERVISOR,
        )

    def add_constraint(
        self,
        task_id: str,
        expected_revision: int,
        content: str,
        *,
        scope: str = "task",
        severity: ConstraintSeverity = ConstraintSeverity.HARD,
    ) -> Constraint:
        return self.store.add_constraint(
            task_id,
            expected_revision,
            content,
            scope=scope,
            severity=severity,
            actor=Actor.USER,
        )

    def supersede_constraint(
        self,
        task_id: str,
        expected_revision: int,
        constraint_id: str,
        *,
        superseded_by: str | None = None,
    ) -> Constraint:
        return self.store.supersede_constraint(
            task_id,
            expected_revision,
            constraint_id,
            superseded_by=superseded_by,
            actor=Actor.USER,
        )

    def create_plan(
        self,
        task_id: str,
        expected_revision: int,
        content: str,
    ) -> Plan:
        return self.store.create_plan(task_id, expected_revision, content, actor=Actor.SUPERVISOR)

    def approve_plan(
        self,
        task_id: str,
        expected_revision: int,
        plan_id: str,
    ) -> Plan:
        return self.store.approve_plan(
            task_id,
            expected_revision,
            plan_id,
            actor=Actor.SUPERVISOR,
        )

    def reject_plan(
        self,
        task_id: str,
        expected_revision: int,
        plan_id: str,
        *,
        reason: str,
    ) -> Plan:
        return self.store.reject_plan(
            task_id,
            expected_revision,
            plan_id,
            reason=reason,
            actor=Actor.SUPERVISOR,
        )

    def latest_plan(self, task_id: str) -> Plan | None:
        return self.store.latest_plan(task_id)

    def approved_plan(self, task_id: str) -> Plan | None:
        return self.store.approved_plan(task_id)

    def bind_kandev_task(
        self,
        task_id: str,
        expected_revision: int,
        kandev_task_id: str,
        *,
        external_id: str,
    ) -> TaskMemory:
        return bind_kandev_task(
            self.store,
            task_id,
            expected_revision,
            kandev_task_id,
            external_id=external_id,
        )

    def assert_revision(self, task_id: str, expected_revision: int) -> TaskMemory:
        task = self.store.get_task(task_id)
        if task.revision != expected_revision:
            raise StaleRevisionError(task_id, expected_revision, task.revision)
        return task
