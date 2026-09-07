from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from pydantic import BaseModel

from .models import Actor, EventType, TaskMemory, TaskPhase, utcnow
from .store import MemoryStore, _dt, _iso

logger = logging.getLogger(__name__)

ACTIVE_RUNTIME_STATUSES = frozenset(
    {
        "planning",
        "executing",
        "running",
        "inprogress",
        "in_progress",
        "started",
    }
)
PLAN_RUNTIME_STATUSES = frozenset({"planning"})
EXECUTION_RUNTIME_STATUSES = frozenset(
    ACTIVE_RUNTIME_STATUSES - PLAN_RUNTIME_STATUSES
)


def is_plan_runtime(status: str | None) -> bool:
    return (status or "").strip().lower() in PLAN_RUNTIME_STATUSES


def is_execution_runtime(status: str | None) -> bool:
    return (status or "").strip().lower() in EXECUTION_RUNTIME_STATUSES


def is_active_runtime(status: str | None) -> bool:
    return (status or "").strip().lower() in ACTIVE_RUNTIME_STATUSES


class CodexRuntimeState(BaseModel):
    task_id: str
    workflow_id: str | None = None
    operation_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    remote_status: str | None = None
    next_action: str | None = None
    last_client_request_id: str | None = None
    runtime_instance_id: str | None = None
    runtime_epoch: int = 0
    runtime_ownership: str = "UNKNOWN"
    isolation_verified: bool = False
    circuit_state: str = "CLOSED"
    circuit_reason: str | None = None
    interrupt_attempted: bool = False
    last_meaningful_event_at: datetime | None = None
    last_status_transition_at: datetime | None = None
    last_semantic_fingerprint: str | None = None
    last_observed_event_count: int = 0
    updated_at: datetime

    @property
    def affinity_verified(self) -> bool:
        return (
            self.runtime_ownership == "SUPERVISOR_MANAGED"
            and self.isolation_verified
            and bool(self.runtime_instance_id)
            and self.runtime_epoch >= 1
        )

    @property
    def circuit_open(self) -> bool:
        return self.circuit_state != "CLOSED"


class CodexRuntimeCircuitState(str, Enum):
    CLOSED = "CLOSED"
    OPEN = "OPEN"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED"


class CodexRuntimeAffinityError(RuntimeError):
    """A task runtime identity does not belong to the current app-server epoch."""


class CodexRuntimeCircuitOpenError(RuntimeError):
    """New Codex control is blocked until verified runtime recovery completes."""


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
        runtime_instance_id=row["runtime_instance_id"],
        runtime_epoch=row["runtime_epoch"],
        runtime_ownership=row["runtime_ownership"],
        isolation_verified=bool(row["isolation_verified"]),
        circuit_state=row["circuit_state"],
        circuit_reason=row["circuit_reason"],
        interrupt_attempted=bool(row["interrupt_attempted"]),
        last_meaningful_event_at=(
            _dt(row["last_meaningful_event_at"])
            if row["last_meaningful_event_at"]
            else None
        ),
        last_status_transition_at=(
            _dt(row["last_status_transition_at"])
            if row["last_status_transition_at"]
            else None
        ),
        last_semantic_fingerprint=row["last_semantic_fingerprint"],
        last_observed_event_count=row["last_observed_event_count"],
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
    runtime_instance_id: str | None = None,
    runtime_epoch: int | None = None,
    runtime_ownership: str | None = None,
    isolation_verified: bool | None = None,
    circuit_state: str | None = None,
    circuit_reason: str | None = None,
    interrupt_attempted: bool | None = None,
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

        def choose(column: str, supplied: Any) -> Any:
            if supplied is not None:
                return supplied
            return existing[column] if existing is not None else None

        existing_instance = existing["runtime_instance_id"] if existing is not None else None
        effective_instance = choose("runtime_instance_id", runtime_instance_id)
        replacing_runtime = (
            runtime_instance_id is not None
            and existing_instance is not None
            and runtime_instance_id != existing_instance
        )
        existing_epoch = int(existing["runtime_epoch"]) if existing is not None else 0
        effective_epoch = int(choose("runtime_epoch", runtime_epoch) or 0)
        if replacing_runtime and effective_epoch <= existing_epoch:
            raise CodexRuntimeAffinityError(
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED: replacement runtime epoch must increase"
            )
        effective_thread = thread_id if replacing_runtime else choose("thread_id", thread_id)
        effective_turn = turn_id if replacing_runtime else choose("turn_id", turn_id)
        values: dict[str, Any] = {}
        if effective_thread is not None or replacing_runtime:
            values["codex_thread_id"] = effective_thread
        if effective_turn is not None or replacing_runtime:
            values["codex_turn_id"] = effective_turn
        if effective_instance is not None or replacing_runtime:
            values["agent_runtime_instance_id"] = effective_instance
            values["agent_runtime_epoch"] = effective_epoch
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
        status = choose("remote_status", remote_status)
        status_transition = (
            existing is None
            or (
                remote_status is not None
                and remote_status != existing["remote_status"]
            )
            or replacing_runtime
        )
        state = CodexRuntimeState(
            task_id=task_id,
            workflow_id=choose("workflow_id", workflow_id),
            operation_id=choose("operation_id", operation_id),
            thread_id=effective_thread,
            turn_id=effective_turn,
            remote_status=status,
            next_action=choose("next_action", next_action),
            last_client_request_id=choose("last_client_request_id", client_request_id),
            runtime_instance_id=effective_instance,
            runtime_epoch=effective_epoch,
            runtime_ownership=choose("runtime_ownership", runtime_ownership) or "UNKNOWN",
            isolation_verified=bool(choose("isolation_verified", isolation_verified) or False),
            circuit_state=(
                circuit_state
                or ("CLOSED" if replacing_runtime else choose("circuit_state", None))
                or "CLOSED"
            ),
            circuit_reason=(
                circuit_reason
                if circuit_reason is not None
                else (None if replacing_runtime else choose("circuit_reason", None))
            ),
            interrupt_attempted=bool(
                interrupt_attempted
                if interrupt_attempted is not None
                else (False if replacing_runtime else choose("interrupt_attempted", None))
            ),
            last_meaningful_event_at=(
                _dt(now)
                if existing is None or replacing_runtime
                else (
                    _dt(existing["last_meaningful_event_at"])
                    if existing["last_meaningful_event_at"]
                    else _dt(now)
                )
            ),
            last_status_transition_at=(
                _dt(now)
                if status_transition
                else (
                    _dt(existing["last_status_transition_at"])
                    if existing is not None and existing["last_status_transition_at"]
                    else _dt(now)
                )
            ),
            last_semantic_fingerprint=(
                None if replacing_runtime else choose("last_semantic_fingerprint", None)
            ),
            last_observed_event_count=(
                0 if replacing_runtime else int(choose("last_observed_event_count", None) or 0)
            ),
            updated_at=_dt(now),
        )
        conn.execute(
            """
            INSERT INTO codex_runtime_state(
                task_id, workflow_id, operation_id, thread_id, turn_id,
                remote_status, next_action, last_client_request_id,
                runtime_instance_id, runtime_epoch, runtime_ownership,
                isolation_verified, circuit_state, circuit_reason,
                interrupt_attempted, last_meaningful_event_at,
                last_status_transition_at, last_semantic_fingerprint,
                last_observed_event_count, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                workflow_id=excluded.workflow_id,
                operation_id=excluded.operation_id,
                thread_id=excluded.thread_id,
                turn_id=excluded.turn_id,
                remote_status=excluded.remote_status,
                next_action=excluded.next_action,
                last_client_request_id=excluded.last_client_request_id,
                runtime_instance_id=excluded.runtime_instance_id,
                runtime_epoch=excluded.runtime_epoch,
                runtime_ownership=excluded.runtime_ownership,
                isolation_verified=excluded.isolation_verified,
                circuit_state=excluded.circuit_state,
                circuit_reason=excluded.circuit_reason,
                interrupt_attempted=excluded.interrupt_attempted,
                last_meaningful_event_at=excluded.last_meaningful_event_at,
                last_status_transition_at=excluded.last_status_transition_at,
                last_semantic_fingerprint=excluded.last_semantic_fingerprint,
                last_observed_event_count=excluded.last_observed_event_count,
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
                state.runtime_instance_id,
                state.runtime_epoch,
                state.runtime_ownership,
                int(state.isolation_verified),
                state.circuit_state,
                state.circuit_reason,
                int(state.interrupt_attempted),
                _iso(state.last_meaningful_event_at),
                _iso(state.last_status_transition_at),
                state.last_semantic_fingerprint,
                state.last_observed_event_count,
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
            "runtime_instance_id": state.runtime_instance_id,
            "runtime_epoch": state.runtime_epoch,
            "runtime_ownership": state.runtime_ownership,
            "isolation_verified": state.isolation_verified,
            "circuit_state": state.circuit_state,
            "task_phase": task.phase.value,
        }
        payload.update(event_payload or {})
        store._insert_event(conn, task, Actor.CODEX, event_type, payload)
        return task, state


def assert_runtime_affinity(
    runtime: CodexRuntimeState,
    *,
    instance_id: str | None,
    runtime_epoch: int,
    require_verified: bool = True,
) -> None:
    if (
        not instance_id
        or runtime.runtime_instance_id != instance_id
        or runtime.runtime_epoch != runtime_epoch
    ):
        raise CodexRuntimeAffinityError(
            "CODEX_RUNTIME_RECONCILIATION_REQUIRED: runtime instance/epoch mismatch"
        )
    if require_verified and not runtime.affinity_verified:
        raise CodexRuntimeAffinityError(
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: runtime isolation is not verified"
        )


def assert_runtime_circuit_closed(runtime: CodexRuntimeState) -> None:
    if runtime.circuit_open:
        raise CodexRuntimeCircuitOpenError(
            f"CODEX_RUNTIME_CIRCUIT_OPEN: {runtime.circuit_reason or 'runtime recovery required'}"
        )


def open_runtime_circuit(
    store: MemoryStore,
    task_id: str,
    *,
    reason: str,
    remote_status: str | None = None,
    recovery_required: bool = True,
) -> CodexRuntimeState:
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise CodexRuntimeAffinityError("Task has no Codex runtime state")
        conn.execute(
            "UPDATE codex_runtime_state SET circuit_state = ?, circuit_reason = ?, "
            "remote_status = COALESCE(?, remote_status), next_action = ?, updated_at = ? "
            "WHERE task_id = ?",
            (
                CodexRuntimeCircuitState.RECOVERY_REQUIRED.value
                if recovery_required
                else CodexRuntimeCircuitState.OPEN.value,
                reason,
                remote_status,
                "RUNTIME_RECOVERY_REQUIRED" if recovery_required else "USER_ACTION_REQUIRED",
                now,
                task_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    state = _state_from_row(updated)
    logger.warning(
        "runtime circuit opened task_id=%s instance_id=%s epoch=%s reason=%s",
        task_id,
        state.runtime_instance_id,
        state.runtime_epoch,
        reason,
    )
    return state


def mark_protocol_interrupt_attempted(
    store: MemoryStore,
    task_id: str,
    *,
    runtime_instance_id: str | None,
    runtime_epoch: int,
) -> CodexRuntimeState:
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise CodexRuntimeAffinityError("Task has no Codex runtime state")
        runtime = _state_from_row(row)
        assert_runtime_affinity(
            runtime,
            instance_id=runtime_instance_id,
            runtime_epoch=runtime_epoch,
        )
        if runtime.interrupt_attempted:
            raise CodexRuntimeCircuitOpenError(
                "CODEX_RUNTIME_CIRCUIT_OPEN: protocol interrupt was already attempted"
            )
        conn.execute(
            "UPDATE codex_runtime_state SET interrupt_attempted = 1, updated_at = ? "
            "WHERE task_id = ?",
            (now, task_id),
        )
        updated = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    state = _state_from_row(updated)
    logger.info(
        "turn interrupt marked task_id=%s instance_id=%s epoch=%s",
        task_id,
        state.runtime_instance_id,
        state.runtime_epoch,
    )
    return state


def close_runtime_circuit_after_recovery(
    store: MemoryStore,
    task_id: str,
    *,
    runtime_instance_id: str,
    runtime_epoch: int,
    ownership: str,
    isolation_verified: bool,
    runtime_status: str,
) -> CodexRuntimeState:
    if (
        ownership != "SUPERVISOR_MANAGED"
        or not isolation_verified
        or runtime_status != "READY"
    ):
        raise CodexRuntimeAffinityError(
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: recovery is not verified"
        )
    now = _iso(utcnow())
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise CodexRuntimeAffinityError("Task has no Codex runtime state")
        current = _state_from_row(row)
        if runtime_epoch < current.runtime_epoch:
            raise CodexRuntimeAffinityError(
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED: recovery epoch is stale"
            )
        replacing = (
            current.runtime_instance_id != runtime_instance_id
            or current.runtime_epoch != runtime_epoch
        )
        conn.execute(
            "UPDATE codex_runtime_state SET runtime_instance_id = ?, runtime_epoch = ?, "
            "runtime_ownership = ?, isolation_verified = 1, circuit_state = 'CLOSED', "
            "circuit_reason = NULL, interrupt_attempted = 0, next_action = NULL, "
            "workflow_id = CASE WHEN ? THEN NULL ELSE workflow_id END, "
            "operation_id = CASE WHEN ? THEN NULL ELSE operation_id END, "
            "thread_id = CASE WHEN ? THEN NULL ELSE thread_id END, "
            "turn_id = CASE WHEN ? THEN NULL ELSE turn_id END, "
            "remote_status = CASE WHEN ? THEN 'not_reconstructable' ELSE remote_status END, "
            "last_semantic_fingerprint = NULL, last_observed_event_count = 0, "
            "last_meaningful_event_at = ?, last_status_transition_at = ?, updated_at = ? "
            "WHERE task_id = ?",
            (
                runtime_instance_id,
                runtime_epoch,
                ownership,
                replacing,
                replacing,
                replacing,
                replacing,
                replacing,
                now,
                now,
                now,
                task_id,
            ),
        )
        if replacing:
            conn.execute(
                "UPDATE supervised_tasks SET codex_thread_id = NULL, codex_turn_id = NULL, "
                "agent_runtime_instance_id = ?, agent_runtime_epoch = ?, updated_at = ? "
                "WHERE task_id = ?",
                (runtime_instance_id, runtime_epoch, now, task_id),
            )
        updated = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    state = _state_from_row(updated)
    logger.info(
        "runtime recovery verified task_id=%s instance_id=%s epoch=%s",
        task_id,
        state.runtime_instance_id,
        state.runtime_epoch,
    )
    return state


def record_runtime_observation(
    store: MemoryStore,
    task_id: str,
    snapshot: Any,
    *,
    observed_at: datetime | None = None,
    stall_after: timedelta = timedelta(minutes=2),
) -> CodexRuntimeState:
    """Persist semantic progress and open the circuit for a stalled active turn."""

    now_dt = observed_at or utcnow()
    now = _iso(now_dt)
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise CodexRuntimeAffinityError("Task has no Codex runtime state")
        runtime = _state_from_row(row)
        snapshot_instance = getattr(snapshot, "runtime_instance_id", None)
        snapshot_epoch = int(getattr(snapshot, "runtime_epoch", 0) or 0)
        if runtime.runtime_instance_id:
            assert_runtime_affinity(
                runtime,
                instance_id=snapshot_instance,
                runtime_epoch=snapshot_epoch,
            )
        status = str(getattr(snapshot, "status", None) or runtime.remote_status or "unknown")
        fingerprint = _semantic_fingerprint(snapshot, status)
        meaningful = fingerprint != runtime.last_semantic_fingerprint
        status_transition = status != (runtime.remote_status or "")
        last_meaningful = now_dt if meaningful else (runtime.last_meaningful_event_at or now_dt)
        last_transition = now_dt if status_transition else (runtime.last_status_transition_at or now_dt)
        raw_event_count = max(0, int(getattr(snapshot, "raw_event_count", 0) or 0))
        pending = list(getattr(snapshot, "pending_interactions", []) or [])
        plan = getattr(snapshot, "plan", None)
        active = status.strip().lower() in ACTIVE_RUNTIME_STATUSES
        unexpected = bool(getattr(snapshot, "reconciliation_required", False)) or (
            status.strip().lower()
            in {
                "unknown",
                "reconciliation_required",
                "compensation_required",
            }
        )
        stalled = (
            active
            and plan is None
            and not pending
            and raw_event_count >= runtime.last_observed_event_count
            and now_dt - last_meaningful >= stall_after
            and now_dt - last_transition >= stall_after
        )
        circuit_state = runtime.circuit_state
        circuit_reason = runtime.circuit_reason
        next_action = runtime.next_action
        stored_status = status
        if unexpected:
            circuit_state = CodexRuntimeCircuitState.RECOVERY_REQUIRED.value
            circuit_reason = "CODEX_RUNTIME_RECONCILIATION_REQUIRED"
            next_action = "RUNTIME_RECOVERY_REQUIRED"
            logger.warning(
                "unexpected runtime result task_id=%s instance_id=%s epoch=%s status=%s",
                task_id,
                runtime.runtime_instance_id,
                runtime.runtime_epoch,
                status,
            )
        elif stalled:
            circuit_state = CodexRuntimeCircuitState.OPEN.value
            circuit_reason = "CODEX_TURN_STALLED"
            next_action = "USER_ACTION_REQUIRED"
            stored_status = "CODEX_TURN_STALLED"
            logger.warning(
                "turn stalled task_id=%s instance_id=%s epoch=%s",
                task_id,
                runtime.runtime_instance_id,
                runtime.runtime_epoch,
            )
        conn.execute(
            "UPDATE codex_runtime_state SET remote_status = ?, next_action = ?, "
            "circuit_state = ?, circuit_reason = ?, last_meaningful_event_at = ?, "
            "last_status_transition_at = ?, last_semantic_fingerprint = ?, "
            "last_observed_event_count = ?, updated_at = ? WHERE task_id = ?",
            (
                stored_status,
                next_action,
                circuit_state,
                circuit_reason,
                _iso(last_meaningful),
                _iso(last_transition),
                fingerprint,
                raw_event_count,
                now,
                task_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM codex_runtime_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _state_from_row(updated)


def _semantic_fingerprint(snapshot: Any, status: str) -> str:
    plan = getattr(snapshot, "plan", None)
    plan_value = None
    if plan is not None:
        plan_value = {
            "content": getattr(plan, "content", None),
            "status": getattr(plan, "status", None),
            "hash": getattr(plan, "plan_hash", None),
        }
    payload = {
        "status": status,
        "plan": plan_value,
        "completed": list(getattr(snapshot, "completed", []) or []),
        "in_progress": list(getattr(snapshot, "in_progress", []) or []),
        "files_changed": list(getattr(snapshot, "files_changed", []) or []),
        "validation": dict(getattr(snapshot, "validation", {}) or {}),
        "blockers": list(getattr(snapshot, "blockers", []) or []),
        "next_steps": list(getattr(snapshot, "next_steps", []) or []),
        "pending": [
            getattr(item, "interaction_id", None)
            for item in list(getattr(snapshot, "pending_interactions", []) or [])
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
