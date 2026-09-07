from __future__ import annotations

import json
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from uuid import uuid4

from codex_supervisor_bridge.db.migrations import apply_migrations
from codex_supervisor_bridge.db.schema import OPTIONAL_FTS_SQL

from .errors import ConflictError, InvalidTransitionError, StaleRevisionError, TaskNotFoundError
from .models import (
    Actor,
    Constraint,
    ConstraintSeverity,
    ConstraintStatus,
    ContextPackMode,
    ContextSnapshot,
    Decision,
    DecisionStatus,
    EventType,
    Evidence,
    EvidenceType,
    MemorySearchHit,
    Plan,
    PlanStatus,
    SummaryType,
    TaskEvent,
    TaskMemory,
    TaskPhase,
    TaskSummary,
    utcnow,
)

if TYPE_CHECKING:
    from codex_supervisor_bridge.bootstrap.physical import PhysicalPathGuard


def _iso(value: datetime | None = None) -> str:
    return (value or utcnow()).astimezone(timezone.utc).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def _default_path_guard() -> "PhysicalPathGuard":
    from codex_supervisor_bridge.bootstrap.physical import PhysicalPathGuard

    return PhysicalPathGuard()


class MemoryStore:
    """Single-file durable memory store with optimistic revision locking."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        path_guard: PhysicalPathGuard | None = None,
    ) -> None:
        self.path = str(path)
        self.path_guard = path_guard or _default_path_guard()
        if self.path != ":memory:":
            database_path = Path(self.path).expanduser()
            self.path_guard.ensure_directory(database_path.parent, role="path")
            self.path_guard.before_write(database_path, role="path")
            self.path = str(database_path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path,
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn.row_factory = sqlite3.Row
        self._fts_enabled = False
        self._initialize()

    @property
    def fts_enabled(self) -> bool:
        return self._fts_enabled

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "MemoryStore":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def _initialize(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA busy_timeout = 5000")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL")
                self._conn.execute("PRAGMA synchronous = NORMAL")
            apply_migrations(self._conn)
            try:
                self._conn.executescript(OPTIONAL_FTS_SQL)
                self._fts_enabled = True
            except sqlite3.OperationalError:
                self._fts_enabled = False

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except Exception:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _task_row(self, conn: sqlite3.Connection, task_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM supervised_tasks WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    @staticmethod
    def _assert_revision(row: sqlite3.Row, expected_revision: int) -> None:
        current = int(row["revision"])
        if current != expected_revision:
            raise StaleRevisionError(str(row["task_id"]), expected_revision, current)

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskMemory:
        return TaskMemory(
            task_id=row["task_id"],
            title=row["title"],
            repository=row["repository"],
            external_kandev_task_id=row["external_kandev_task_id"],
            status=row["status"],
            phase=TaskPhase(row["phase"]),
            revision=row["revision"],
            intent_version=row["intent_version"],
            plan_version=row["plan_version"],
            current_goal=row["current_goal"],
            current_state=row["current_state"],
            codex_thread_id=row["codex_thread_id"],
            codex_turn_id=row["codex_turn_id"],
            agent_runtime_instance_id=row["agent_runtime_instance_id"],
            agent_runtime_epoch=row["agent_runtime_epoch"],
            git_branch=row["git_branch"],
            git_head=row["git_head"],
            pr_number=row["pr_number"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> TaskEvent:
        return TaskEvent(
            event_id=row["event_id"],
            task_id=row["task_id"],
            actor=Actor(row["actor"]),
            event_type=EventType(row["event_type"]),
            revision=row["revision"],
            intent_version=row["intent_version"],
            plan_version=row["plan_version"],
            payload=json.loads(row["payload_json"]),
            created_at=_dt(row["created_at"]),
        )

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        task: TaskMemory,
        actor: Actor,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
        *,
        event_id: str | None = None,
    ) -> TaskEvent:
        event = TaskEvent(
            event_id=event_id or _id("ev"),
            task_id=task.task_id,
            actor=actor,
            event_type=event_type,
            revision=task.revision,
            intent_version=task.intent_version,
            plan_version=task.plan_version,
            payload=payload or {},
        )
        conn.execute(
            """
            INSERT INTO task_events(
                event_id, task_id, revision, actor, event_type,
                intent_version, plan_version, payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.event_id,
                event.task_id,
                event.revision,
                event.actor.value,
                event.event_type.value,
                event.intent_version,
                event.plan_version,
                _json(event.payload),
                _iso(event.created_at),
            ),
        )
        return event

    def _update_task(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        expected_revision: int,
        *,
        revision_delta: int = 1,
        intent_delta: int = 0,
        plan_delta: int = 0,
        values: dict[str, Any] | None = None,
    ) -> TaskMemory:
        row = self._task_row(conn, task_id)
        self._assert_revision(row, expected_revision)
        allowed = {
            "status",
            "phase",
            "current_goal",
            "current_state",
            "external_kandev_task_id",
            "codex_thread_id",
            "codex_turn_id",
            "agent_runtime_instance_id",
            "agent_runtime_epoch",
            "git_branch",
            "git_head",
            "pr_number",
        }
        updates = dict(values or {})
        unknown = set(updates) - allowed
        if unknown:
            raise ValueError(f"Unsupported task fields: {sorted(unknown)}")
        updates["revision"] = int(row["revision"]) + revision_delta
        updates["intent_version"] = int(row["intent_version"]) + intent_delta
        updates["plan_version"] = int(row["plan_version"]) + plan_delta
        updates["updated_at"] = _iso()
        assignments = ", ".join(f"{name} = ?" for name in updates)
        params = [
            value.value if isinstance(value, TaskPhase) else value
            for value in updates.values()
        ]
        params.extend([task_id, expected_revision])
        result = conn.execute(
            f"UPDATE supervised_tasks SET {assignments} WHERE task_id = ? AND revision = ?",
            params,
        )
        if result.rowcount != 1:
            current = self._task_row(conn, task_id)
            raise StaleRevisionError(task_id, expected_revision, int(current["revision"]))
        return self._task_from_row(self._task_row(conn, task_id))

    def create_task(
        self,
        task_id: str,
        title: str,
        *,
        repository: str | None = None,
        goal: str | None = None,
    ) -> TaskMemory:
        now = _iso()
        with self._write() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO supervised_tasks(
                        task_id, title, repository, current_goal, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, title, repository, goal, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(f"Supervised task already exists: {task_id}") from exc
            task = self._task_from_row(self._task_row(conn, task_id))
            self._insert_event(
                conn,
                task,
                Actor.USER,
                EventType.TASK_CREATED,
                {"title": title, "repository": repository},
            )
            if goal:
                self._insert_event(
                    conn,
                    task,
                    Actor.USER,
                    EventType.INTENT_CREATED,
                    {"goal": goal},
                )
            self._upsert_document(
                conn,
                task_id,
                "task",
                task_id,
                title,
                goal or title,
                task.status,
            )
            return task

    def get_task(self, task_id: str) -> TaskMemory:
        with self._lock:
            return self._task_from_row(self._task_row(self._conn, task_id))

    def list_events(self, task_id: str, *, limit: int = 100) -> list[TaskEvent]:
        self.get_task(task_id)
        rows = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [self._event_from_row(row) for row in reversed(rows)]

    def recent_events(
        self,
        task_id: str,
        *,
        actors: set[Actor] | None = None,
        event_types: set[EventType] | None = None,
        limit: int = 20,
    ) -> list[TaskEvent]:
        events = self.list_events(task_id, limit=max(limit * 5, limit))
        filtered = [
            event
            for event in events
            if (actors is None or event.actor in actors)
            and (event_types is None or event.event_type in event_types)
        ]
        return filtered[-limit:]

    def record_event(
        self,
        task_id: str,
        expected_revision: int,
        actor: Actor,
        event_type: EventType,
        payload: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """Append a supervisor-relevant event and atomically advance task revision."""
        with self._write() as conn:
            task = self._update_task(conn, task_id, expected_revision)
            return self._insert_event(conn, task, actor, event_type, payload)

    def update_intent(
        self,
        task_id: str,
        expected_revision: int,
        goal: str,
        *,
        actor: Actor = Actor.USER,
    ) -> TaskMemory:
        with self._write() as conn:
            task = self._update_task(
                conn,
                task_id,
                expected_revision,
                intent_delta=1,
                values={"current_goal": goal},
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.INTENT_UPDATED,
                {"goal": goal},
            )
            self._upsert_document(conn, task_id, "task", task_id, task.title, goal, task.status)
            return task

    def set_phase(
        self,
        task_id: str,
        expected_revision: int,
        phase: TaskPhase,
        *,
        actor: Actor = Actor.SYSTEM,
        current_state: str | None = None,
    ) -> TaskMemory:
        with self._write() as conn:
            before = self._task_from_row(self._task_row(conn, task_id))
            values: dict[str, Any] = {"phase": phase}
            if current_state is not None:
                values["current_state"] = current_state
            task = self._update_task(conn, task_id, expected_revision, values=values)
            self._insert_event(
                conn,
                task,
                actor,
                EventType.TASK_PHASE_CHANGED,
                {"from": before.phase.value, "to": phase.value},
            )
            return task

    def add_decision(
        self,
        task_id: str,
        expected_revision: int,
        title: str,
        content: str,
        *,
        decision_type: str = "general",
        actor: Actor = Actor.SUPERVISOR,
    ) -> Decision:
        with self._write() as conn:
            task = self._update_task(conn, task_id, expected_revision)
            event_id = _id("ev")
            decision = Decision(
                decision_id=_id("dec"),
                task_id=task_id,
                decision_type=decision_type,
                title=title,
                content=content,
                source_event_id=event_id,
                created_intent_version=task.intent_version,
            )
            conn.execute(
                """
                INSERT INTO task_decisions(
                    decision_id, task_id, decision_type, status, title, content,
                    source_event_id, created_intent_version, superseded_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    decision.decision_id,
                    task_id,
                    decision.decision_type,
                    decision.status.value,
                    decision.title,
                    decision.content,
                    decision.source_event_id,
                    decision.created_intent_version,
                    None,
                    _iso(decision.created_at),
                    _iso(decision.updated_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.DECISION_ADDED,
                {"decision_id": decision.decision_id, "title": title, "content": content},
                event_id=event_id,
            )
            self._upsert_document(
                conn,
                task_id,
                "decision",
                decision.decision_id,
                title,
                content,
                decision.status.value,
            )
            return decision

    def supersede_decision(
        self,
        task_id: str,
        expected_revision: int,
        decision_id: str,
        *,
        superseded_by: str | None = None,
        actor: Actor = Actor.SUPERVISOR,
    ) -> Decision:
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM task_decisions WHERE task_id = ? AND decision_id = ?",
                (task_id, decision_id),
            ).fetchone()
            if row is None:
                raise ConflictError(f"Unknown decision: {decision_id}")
            if row["status"] != DecisionStatus.ACTIVE.value:
                raise InvalidTransitionError(f"Decision is not ACTIVE: {decision_id}")
            task = self._update_task(conn, task_id, expected_revision)
            now = _iso()
            conn.execute(
                "UPDATE task_decisions SET status = ?, superseded_by = ?, updated_at = ? "
                "WHERE decision_id = ?",
                (DecisionStatus.SUPERSEDED.value, superseded_by, now, decision_id),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.DECISION_SUPERSEDED,
                {"decision_id": decision_id, "superseded_by": superseded_by},
            )
            updated = conn.execute(
                "SELECT * FROM task_decisions WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            assert updated is not None
            self._upsert_document(
                conn,
                task_id,
                "decision",
                decision_id,
                updated["title"],
                updated["content"],
                updated["status"],
            )
            return self._decision_from_row(updated)

    def active_decisions(self, task_id: str) -> list[Decision]:
        self.get_task(task_id)
        rows = self._conn.execute(
            "SELECT * FROM task_decisions WHERE task_id = ? AND status = ? "
            "ORDER BY created_at, decision_id",
            (task_id, DecisionStatus.ACTIVE.value),
        ).fetchall()
        return [self._decision_from_row(row) for row in rows]

    @staticmethod
    def _decision_from_row(row: sqlite3.Row) -> Decision:
        return Decision(
            decision_id=row["decision_id"],
            task_id=row["task_id"],
            decision_type=row["decision_type"],
            status=DecisionStatus(row["status"]),
            title=row["title"],
            content=row["content"],
            source_event_id=row["source_event_id"],
            created_intent_version=row["created_intent_version"],
            superseded_by=row["superseded_by"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def add_constraint(
        self,
        task_id: str,
        expected_revision: int,
        content: str,
        *,
        scope: str = "task",
        severity: ConstraintSeverity = ConstraintSeverity.HARD,
        actor: Actor = Actor.USER,
    ) -> Constraint:
        with self._write() as conn:
            task = self._update_task(conn, task_id, expected_revision)
            event_id = _id("ev")
            constraint = Constraint(
                constraint_id=_id("con"),
                task_id=task_id,
                scope=scope,
                severity=severity,
                content=content,
                source_event_id=event_id,
                created_revision=task.revision,
            )
            conn.execute(
                """
                INSERT INTO task_constraints(
                    constraint_id, task_id, scope, severity, status, content,
                    source_event_id, created_revision, superseded_by, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    constraint.constraint_id,
                    task_id,
                    constraint.scope,
                    constraint.severity.value,
                    constraint.status.value,
                    constraint.content,
                    constraint.source_event_id,
                    constraint.created_revision,
                    None,
                    _iso(constraint.created_at),
                    _iso(constraint.updated_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.CONSTRAINT_ADDED,
                {
                    "constraint_id": constraint.constraint_id,
                    "scope": scope,
                    "severity": severity.value,
                    "content": content,
                },
                event_id=event_id,
            )
            self._upsert_document(
                conn,
                task_id,
                "constraint",
                constraint.constraint_id,
                scope,
                content,
                constraint.status.value,
            )
            return constraint

    def supersede_constraint(
        self,
        task_id: str,
        expected_revision: int,
        constraint_id: str,
        *,
        superseded_by: str | None = None,
        actor: Actor = Actor.USER,
    ) -> Constraint:
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM task_constraints WHERE task_id = ? AND constraint_id = ?",
                (task_id, constraint_id),
            ).fetchone()
            if row is None:
                raise ConflictError(f"Unknown constraint: {constraint_id}")
            if row["status"] != ConstraintStatus.ACTIVE.value:
                raise InvalidTransitionError(f"Constraint is not ACTIVE: {constraint_id}")
            task = self._update_task(conn, task_id, expected_revision)
            now = _iso()
            conn.execute(
                "UPDATE task_constraints SET status = ?, superseded_by = ?, updated_at = ? "
                "WHERE constraint_id = ?",
                (ConstraintStatus.SUPERSEDED.value, superseded_by, now, constraint_id),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.CONSTRAINT_SUPERSEDED,
                {"constraint_id": constraint_id, "superseded_by": superseded_by},
            )
            updated = conn.execute(
                "SELECT * FROM task_constraints WHERE constraint_id = ?",
                (constraint_id,),
            ).fetchone()
            assert updated is not None
            self._upsert_document(
                conn,
                task_id,
                "constraint",
                constraint_id,
                updated["scope"],
                updated["content"],
                updated["status"],
            )
            return self._constraint_from_row(updated)

    def active_constraints(self, task_id: str) -> list[Constraint]:
        self.get_task(task_id)
        rows = self._conn.execute(
            "SELECT * FROM task_constraints WHERE task_id = ? AND status = ? "
            "ORDER BY CASE severity WHEN 'HARD' THEN 0 WHEN 'SOFT' THEN 1 ELSE 2 END, "
            "created_revision, constraint_id",
            (task_id, ConstraintStatus.ACTIVE.value),
        ).fetchall()
        return [self._constraint_from_row(row) for row in rows]

    @staticmethod
    def _constraint_from_row(row: sqlite3.Row) -> Constraint:
        return Constraint(
            constraint_id=row["constraint_id"],
            task_id=row["task_id"],
            scope=row["scope"],
            severity=ConstraintSeverity(row["severity"]),
            status=ConstraintStatus(row["status"]),
            content=row["content"],
            source_event_id=row["source_event_id"],
            created_revision=row["created_revision"],
            superseded_by=row["superseded_by"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def create_plan(
        self,
        task_id: str,
        expected_revision: int,
        content: str,
        *,
        actor: Actor = Actor.CODEX,
    ) -> Plan:
        with self._write() as conn:
            task = self._update_task(
                conn,
                task_id,
                expected_revision,
                plan_delta=1,
                values={"phase": TaskPhase.PLAN_REVIEW},
            )
            event_id = _id("ev")
            plan = Plan(
                plan_id=_id("plan"),
                task_id=task_id,
                plan_version=task.plan_version,
                content=content,
                source_event_id=event_id,
            )
            conn.execute(
                """
                INSERT INTO task_plans(
                    plan_id, task_id, plan_version, status, content,
                    source_event_id, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    task_id,
                    plan.plan_version,
                    plan.status.value,
                    plan.content,
                    plan.source_event_id,
                    _iso(plan.created_at),
                    _iso(plan.updated_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.PLAN_CREATED,
                {"plan_id": plan.plan_id, "plan_version": plan.plan_version},
                event_id=event_id,
            )
            self._upsert_document(
                conn,
                task_id,
                "plan",
                plan.plan_id,
                f"Plan V{plan.plan_version}",
                content,
                plan.status.value,
            )
            return plan

    def approve_plan(
        self,
        task_id: str,
        expected_revision: int,
        plan_id: str,
        *,
        actor: Actor = Actor.SUPERVISOR,
    ) -> Plan:
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM task_plans WHERE task_id = ? AND plan_id = ?",
                (task_id, plan_id),
            ).fetchone()
            if row is None:
                raise ConflictError(f"Unknown plan: {plan_id}")
            if row["status"] != PlanStatus.DRAFT.value:
                raise InvalidTransitionError(f"Plan is not DRAFT: {plan_id}")
            task = self._update_task(
                conn,
                task_id,
                expected_revision,
                values={"phase": TaskPhase.IMPLEMENTING},
            )
            now = _iso()
            old_rows = conn.execute(
                "SELECT plan_id, plan_version, content FROM task_plans "
                "WHERE task_id = ? AND status = ?",
                (task_id, PlanStatus.APPROVED.value),
            ).fetchall()
            conn.execute(
                "UPDATE task_plans SET status = ?, updated_at = ? "
                "WHERE task_id = ? AND status = ?",
                (PlanStatus.SUPERSEDED.value, now, task_id, PlanStatus.APPROVED.value),
            )
            for old in old_rows:
                self._insert_event(
                    conn,
                    task,
                    actor,
                    EventType.PLAN_SUPERSEDED,
                    {"plan_id": old["plan_id"], "superseded_by": plan_id},
                )
                self._upsert_document(
                    conn,
                    task_id,
                    "plan",
                    old["plan_id"],
                    f"Plan V{old['plan_version']}",
                    old["content"],
                    PlanStatus.SUPERSEDED.value,
                )
            conn.execute(
                "UPDATE task_plans SET status = ?, updated_at = ? WHERE plan_id = ?",
                (PlanStatus.APPROVED.value, now, plan_id),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.PLAN_APPROVED,
                {"plan_id": plan_id, "plan_version": row["plan_version"]},
            )
            updated = conn.execute(
                "SELECT * FROM task_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            assert updated is not None
            plan = self._plan_from_row(updated)
            self._upsert_document(
                conn,
                task_id,
                "plan",
                plan.plan_id,
                f"Plan V{plan.plan_version}",
                plan.content,
                plan.status.value,
            )
            return plan

    def reject_plan(
        self,
        task_id: str,
        expected_revision: int,
        plan_id: str,
        *,
        reason: str,
        actor: Actor = Actor.SUPERVISOR,
    ) -> Plan:
        with self._write() as conn:
            row = conn.execute(
                "SELECT * FROM task_plans WHERE task_id = ? AND plan_id = ?",
                (task_id, plan_id),
            ).fetchone()
            if row is None:
                raise ConflictError(f"Unknown plan: {plan_id}")
            if row["status"] != PlanStatus.DRAFT.value:
                raise InvalidTransitionError(f"Plan is not DRAFT: {plan_id}")
            task = self._update_task(
                conn,
                task_id,
                expected_revision,
                values={"phase": TaskPhase.PLANNING},
            )
            now = _iso()
            conn.execute(
                "UPDATE task_plans SET status = ?, updated_at = ? WHERE plan_id = ?",
                (PlanStatus.REJECTED.value, now, plan_id),
            )
            self._insert_event(
                conn,
                task,
                actor,
                EventType.PLAN_REJECTED,
                {"plan_id": plan_id, "reason": reason},
            )
            updated = conn.execute(
                "SELECT * FROM task_plans WHERE plan_id = ?",
                (plan_id,),
            ).fetchone()
            assert updated is not None
            plan = self._plan_from_row(updated)
            self._upsert_document(
                conn,
                task_id,
                "plan",
                plan.plan_id,
                f"Plan V{plan.plan_version}",
                plan.content,
                plan.status.value,
            )
            return plan

    def approved_plan(self, task_id: str) -> Plan | None:
        self.get_task(task_id)
        row = self._conn.execute(
            "SELECT * FROM task_plans WHERE task_id = ? AND status = ? "
            "ORDER BY plan_version DESC LIMIT 1",
            (task_id, PlanStatus.APPROVED.value),
        ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    def latest_plan(self, task_id: str) -> Plan | None:
        self.get_task(task_id)
        row = self._conn.execute(
            "SELECT * FROM task_plans WHERE task_id = ? ORDER BY plan_version DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self._plan_from_row(row) if row is not None else None

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> Plan:
        return Plan(
            plan_id=row["plan_id"],
            task_id=row["task_id"],
            plan_version=row["plan_version"],
            status=PlanStatus(row["status"]),
            content=row["content"],
            source_event_id=row["source_event_id"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    def add_summary(
        self,
        task_id: str,
        summary_type: SummaryType,
        from_revision: int,
        to_revision: int,
        content: str,
    ) -> TaskSummary:
        if to_revision < from_revision:
            raise ValueError("to_revision must be >= from_revision")
        with self._write() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            if to_revision > task.revision:
                raise ValueError("summary cannot cover a future revision")
            summary = TaskSummary(
                summary_id=_id("sum"),
                task_id=task_id,
                summary_type=summary_type,
                from_revision=from_revision,
                to_revision=to_revision,
                content=content,
            )
            conn.execute(
                """
                INSERT INTO task_summaries(
                    summary_id, task_id, summary_type, from_revision,
                    to_revision, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    summary.summary_id,
                    task_id,
                    summary.summary_type.value,
                    from_revision,
                    to_revision,
                    content,
                    _iso(summary.created_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                Actor.SYSTEM,
                EventType.SUMMARY_CREATED,
                {"summary_id": summary.summary_id, "summary_type": summary_type.value},
            )
            self._upsert_document(
                conn,
                task_id,
                "summary",
                summary.summary_id,
                summary_type.value,
                content,
                None,
            )
            return summary

    def latest_summary(self, task_id: str, summary_type: SummaryType) -> TaskSummary | None:
        self.get_task(task_id)
        row = self._conn.execute(
            "SELECT * FROM task_summaries WHERE task_id = ? AND summary_type = ? "
            "ORDER BY to_revision DESC, created_at DESC LIMIT 1",
            (task_id, summary_type.value),
        ).fetchone()
        if row is None:
            return None
        return TaskSummary(
            summary_id=row["summary_id"],
            task_id=row["task_id"],
            summary_type=SummaryType(row["summary_type"]),
            from_revision=row["from_revision"],
            to_revision=row["to_revision"],
            content=row["content"],
            created_at=_dt(row["created_at"]),
        )

    def add_evidence(
        self,
        task_id: str,
        evidence_type: EvidenceType,
        source: str,
        summary: str,
        *,
        external_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        created_revision: int | None = None,
    ) -> Evidence:
        with self._write() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            revision = task.revision if created_revision is None else created_revision
            if revision > task.revision:
                raise ValueError("evidence cannot reference a future revision")
            evidence = Evidence(
                evidence_id=_id("evi"),
                task_id=task_id,
                evidence_type=evidence_type,
                source=source,
                external_id=external_id,
                summary=summary,
                metadata=metadata or {},
                created_revision=revision,
            )
            conn.execute(
                """
                INSERT INTO evidence_index(
                    evidence_id, task_id, evidence_type, source, external_id,
                    summary, metadata_json, created_revision, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence.evidence_id,
                    task_id,
                    evidence.evidence_type.value,
                    source,
                    external_id,
                    summary,
                    _json(evidence.metadata),
                    revision,
                    _iso(evidence.created_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                Actor.SYSTEM,
                EventType.EVIDENCE_ADDED,
                {"evidence_id": evidence.evidence_id, "type": evidence_type.value},
            )
            self._upsert_document(
                conn,
                task_id,
                "evidence",
                evidence.evidence_id,
                evidence_type.value,
                summary,
                None,
            )
            return evidence

    def recent_evidence(self, task_id: str, *, limit: int = 10) -> list[Evidence]:
        self.get_task(task_id)
        rows = self._conn.execute(
            "SELECT * FROM evidence_index WHERE task_id = ? "
            "ORDER BY created_revision DESC, created_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [
            Evidence(
                evidence_id=row["evidence_id"],
                task_id=row["task_id"],
                evidence_type=EvidenceType(row["evidence_type"]),
                source=row["source"],
                external_id=row["external_id"],
                summary=row["summary"],
                metadata=json.loads(row["metadata_json"]),
                created_revision=row["created_revision"],
                created_at=_dt(row["created_at"]),
            )
            for row in rows
        ]

    def save_context_snapshot(
        self,
        task_id: str,
        mode: ContextPackMode,
        content: str,
        token_estimate: int,
    ) -> ContextSnapshot:
        with self._write() as conn:
            task = self._task_from_row(self._task_row(conn, task_id))
            snapshot = ContextSnapshot(
                snapshot_id=_id("ctx"),
                task_id=task_id,
                revision=task.revision,
                intent_version=task.intent_version,
                plan_version=task.plan_version,
                mode=mode,
                token_estimate=token_estimate,
                content=content,
            )
            conn.execute(
                """
                INSERT INTO context_snapshots(
                    snapshot_id, task_id, revision, intent_version, plan_version,
                    mode, token_estimate, content, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    task_id,
                    snapshot.revision,
                    snapshot.intent_version,
                    snapshot.plan_version,
                    snapshot.mode.value,
                    snapshot.token_estimate,
                    snapshot.content,
                    _iso(snapshot.created_at),
                ),
            )
            self._insert_event(
                conn,
                task,
                Actor.SYSTEM,
                EventType.CONTEXT_SNAPSHOT_CREATED,
                {"snapshot_id": snapshot.snapshot_id, "mode": mode.value},
            )
            return snapshot

    def _upsert_document(
        self,
        conn: sqlite3.Connection,
        task_id: str,
        kind: str,
        source_id: str,
        title: str,
        content: str,
        status: str | None,
    ) -> None:
        now = _iso()
        document_id = f"{task_id}:{kind}:{source_id}"
        conn.execute(
            """
            INSERT INTO memory_documents(
                document_id, task_id, kind, source_id, title, content,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id, kind, source_id) DO UPDATE SET
                title=excluded.title,
                content=excluded.content,
                status=excluded.status,
                updated_at=excluded.updated_at
            """,
            (document_id, task_id, kind, source_id, title, content, status, now, now),
        )
        if self._fts_enabled:
            conn.execute(
                "DELETE FROM memory_fts WHERE task_id = ? AND kind = ? AND source_id = ?",
                (task_id, kind, source_id),
            )
            conn.execute(
                "INSERT INTO memory_fts(task_id, kind, source_id, title, content) "
                "VALUES (?, ?, ?, ?, ?)",
                (task_id, kind, source_id, title, content),
            )

    @staticmethod
    def _fts_query(query: str) -> str:
        terms = [term for term in re.split(r"\s+", query.strip()) if term]
        if not terms:
            return '""'
        escaped = [term.replace('"', '""') for term in terms]
        return " OR ".join(f'"{term}"' for term in escaped)

    def search(self, task_id: str, query: str, *, limit: int = 10) -> list[MemorySearchHit]:
        self.get_task(task_id)
        if not query.strip():
            return []
        if self._fts_enabled:
            rows = self._conn.execute(
                """
                SELECT f.kind, f.source_id, f.title, f.content, d.status,
                       bm25(memory_fts) AS score
                FROM memory_fts AS f
                JOIN memory_documents AS d
                  ON d.task_id = f.task_id
                 AND d.kind = f.kind
                 AND d.source_id = f.source_id
                WHERE memory_fts MATCH ? AND f.task_id = ?
                ORDER BY score
                LIMIT ?
                """,
                (self._fts_query(query), task_id, limit),
            ).fetchall()
        else:
            pattern = f"%{query.strip()}%"
            rows = self._conn.execute(
                """
                SELECT kind, source_id, title, content, status, NULL AS score
                FROM memory_documents
                WHERE task_id = ? AND (title LIKE ? OR content LIKE ?)
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (task_id, pattern, pattern, limit),
            ).fetchall()
        return [
            MemorySearchHit(
                kind=row["kind"],
                source_id=row["source_id"],
                title=row["title"],
                content=row["content"],
                status=row["status"],
                score=row["score"],
            )
            for row in rows
        ]
