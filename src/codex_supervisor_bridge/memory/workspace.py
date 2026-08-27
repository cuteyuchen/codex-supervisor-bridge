from __future__ import annotations

import json
from typing import Any

from .errors import ConflictError
from .models import ActiveWriter, Actor, EventType, ExecutionMode, TaskMemory, utcnow
from .store import MemoryStore, _dt, _id, _iso, _json
from .workspace_models import (
    DirectOperationCompleteResult,
    DirectOperationPrepareResult,
    DirectOperationStatus,
    DirectWorkspaceOperation,
    WorkspaceBinding,
    WorkspaceBindingStatus,
)


def _binding_from_row(row: Any) -> WorkspaceBinding:
    return WorkspaceBinding(
        task_id=row["task_id"],
        backend_name=row["backend_name"],
        workspace_id=row["workspace_id"],
        repository=row["repository"],
        root=row["root"],
        workspace_mode=row["workspace_mode"],
        base_ref=row["base_ref"],
        git_branch=row["git_branch"],
        git_head=row["git_head"],
        dirty=bool(row["dirty"]),
        changed_files=json.loads(row["changed_files_json"] or "[]"),
        last_review_ref=row["last_review_ref"],
        state=WorkspaceBindingStatus(row["state"]),
        updated_at=_dt(row["updated_at"]),
    )


def _operation_from_row(row: Any) -> DirectWorkspaceOperation:
    return DirectWorkspaceOperation(
        operation_id=row["operation_id"],
        task_id=row["task_id"],
        operation_type=row["operation_type"],
        status=DirectOperationStatus(row["status"]),
        writer_epoch=row["writer_epoch"],
        prepared_revision=row["prepared_revision"],
        completed_revision=row["completed_revision"],
        request_digest=row["request_digest"],
        summary=row["summary"],
        change_ref=row["change_ref"],
        git_head_before=row["git_head_before"],
        git_head_after=row["git_head_after"],
        details=json.loads(row["details_json"] or "{}"),
        created_at=_dt(row["created_at"]),
        updated_at=_dt(row["updated_at"]),
    )


def get_workspace_binding(store: MemoryStore, task_id: str) -> WorkspaceBinding | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
    return _binding_from_row(row) if row is not None else None


def get_prepared_direct_operation(
    store: MemoryStore,
    task_id: str,
) -> DirectWorkspaceOperation | None:
    store.get_task(task_id)
    with store._lock:
        row = store._conn.execute(
            "SELECT * FROM direct_workspace_operations "
            "WHERE task_id = ? AND status = 'PREPARED' ORDER BY created_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    return _operation_from_row(row) if row is not None else None


def bind_workspace(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    *,
    backend_name: str,
    workspace_id: str,
    repository: str,
    root: str | None,
    workspace_mode: str,
    base_ref: str | None = None,
    git_branch: str | None = None,
    git_head: str | None = None,
) -> tuple[TaskMemory, WorkspaceBinding]:
    if workspace_mode not in {"checkout", "worktree"}:
        raise ValueError("workspace_mode must be checkout or worktree")
    if not backend_name.strip() or not workspace_id.strip() or not repository.strip():
        raise ValueError("backend_name, workspace_id and repository must not be empty")
    now_text = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        task_before = store._task_from_row(task_row)
        execution = conn.execute(
            "SELECT active_writer FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if execution is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        existing_row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if existing_row is not None:
            existing = _binding_from_row(existing_row)
            if (
                existing.backend_name == backend_name
                and existing.workspace_id == workspace_id
                and existing.repository == repository
                and existing.state == WorkspaceBindingStatus.ACTIVE
            ):
                return task_before, existing
            if ActiveWriter(execution["active_writer"]) != ActiveWriter.NONE:
                raise ConflictError("Release the current writer before replacing the supervised workspace")
            if conn.execute(
                "SELECT 1 FROM direct_workspace_operations "
                "WHERE task_id = ? AND status = 'PREPARED'",
                (task_id,),
            ).fetchone():
                raise ConflictError("A direct workspace operation is still in progress")

        task = store._update_task(
            conn,
            task_id,
            expected_revision,
            values={"git_branch": git_branch, "git_head": git_head},
        )
        conn.execute(
            """
            INSERT INTO task_workspace_state(
                task_id, backend_name, workspace_id, repository, root,
                workspace_mode, base_ref, git_branch, git_head, dirty,
                changed_files_json, last_review_ref, state, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', NULL, 'ACTIVE', ?)
            ON CONFLICT(task_id) DO UPDATE SET
                backend_name=excluded.backend_name,
                workspace_id=excluded.workspace_id,
                repository=excluded.repository,
                root=excluded.root,
                workspace_mode=excluded.workspace_mode,
                base_ref=excluded.base_ref,
                git_branch=excluded.git_branch,
                git_head=excluded.git_head,
                dirty=0,
                changed_files_json='[]',
                last_review_ref=NULL,
                state='ACTIVE',
                updated_at=excluded.updated_at
            """,
            (
                task_id,
                backend_name,
                workspace_id,
                repository,
                root,
                workspace_mode,
                base_ref,
                git_branch,
                git_head,
                now_text,
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {
                "kind": "WORKSPACE_BOUND",
                "backend": backend_name,
                "workspace_id": workspace_id,
                "workspace_mode": workspace_mode,
                "base_ref": base_ref,
                "git_head": git_head,
            },
        )
        row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return task, _binding_from_row(row)


def update_workspace_derived(
    store: MemoryStore,
    task_id: str,
    *,
    git_branch: str | None = None,
    git_head: str | None = None,
    dirty: bool | None = None,
    changed_files: list[str] | None = None,
    review_ref: str | None = None,
) -> WorkspaceBinding:
    """Refresh observed workspace facts without churning the task revision."""
    now_text = _iso()
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if row is None:
            raise ConflictError(f"No supervised workspace is bound for task {task_id}")
        current = _binding_from_row(row)
        conn.execute(
            """
            UPDATE task_workspace_state
            SET git_branch = ?, git_head = ?, dirty = ?, changed_files_json = ?,
                last_review_ref = ?, updated_at = ?
            WHERE task_id = ?
            """,
            (
                git_branch if git_branch is not None else current.git_branch,
                git_head if git_head is not None else current.git_head,
                int(dirty if dirty is not None else current.dirty),
                _json(changed_files if changed_files is not None else current.changed_files),
                review_ref if review_ref is not None else current.last_review_ref,
                now_text,
                task_id,
            ),
        )
        updated = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        return _binding_from_row(updated)


def prepare_direct_operation(
    store: MemoryStore,
    task_id: str,
    expected_revision: int,
    expected_writer_epoch: int,
    *,
    operation_type: str,
    request_digest: str,
    details: dict[str, Any] | None = None,
) -> DirectOperationPrepareResult:
    if not operation_type.strip() or not request_digest.strip():
        raise ValueError("operation_type and request_digest must not be empty")
    now = utcnow()
    now_text = _iso(now)
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        store._assert_revision(task_row, expected_revision)
        execution = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if execution is None:
            raise ConflictError(f"Task execution state is missing: {task_id}")
        if ActiveWriter(execution["active_writer"]) != ActiveWriter.CHATGPT:
            raise ConflictError("Direct workspace mutation requires CHATGPT to own the writer lease")
        if execution["writer_epoch"] != expected_writer_epoch:
            raise ConflictError(
                f"Writer epoch mismatch: expected={expected_writer_epoch}, "
                f"current={execution['writer_epoch']}"
            )
        if ExecutionMode(execution["execution_mode"]) == ExecutionMode.CODEX_SUPERVISED:
            raise ConflictError("Direct workspace mutation is disabled in CODEX_SUPERVISED mode")
        workspace_row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if workspace_row is None:
            raise ConflictError("Open and bind a supervised workspace before direct mutation")
        workspace = _binding_from_row(workspace_row)
        if workspace.state != WorkspaceBindingStatus.ACTIVE:
            raise ConflictError(
                "Workspace requires reconciliation before another direct mutation"
            )
        if conn.execute(
            "SELECT 1 FROM direct_workspace_operations "
            "WHERE task_id = ? AND status = 'PREPARED'",
            (task_id,),
        ).fetchone():
            raise ConflictError("Another direct workspace operation is still in progress")

        task = store._update_task(conn, task_id, expected_revision)
        operation = DirectWorkspaceOperation(
            operation_id=_id("directop"),
            task_id=task_id,
            operation_type=operation_type.strip(),
            status=DirectOperationStatus.PREPARED,
            writer_epoch=expected_writer_epoch,
            prepared_revision=task.revision,
            request_digest=request_digest.strip(),
            git_head_before=workspace.git_head,
            details=details or {},
            created_at=now,
            updated_at=now,
        )
        conn.execute(
            """
            INSERT INTO direct_workspace_operations(
                operation_id, task_id, operation_type, status, writer_epoch,
                prepared_revision, completed_revision, request_digest, summary,
                change_ref, git_head_before, git_head_after, details_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, NULL, ?, ?, ?)
            """,
            (
                operation.operation_id,
                task_id,
                operation.operation_type,
                operation.status.value,
                operation.writer_epoch,
                operation.prepared_revision,
                operation.request_digest,
                operation.git_head_before,
                _json(operation.details),
                now_text,
                now_text,
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {
                "kind": "DIRECT_WORKSPACE_OPERATION_PREPARED",
                "operation_id": operation.operation_id,
                "operation_type": operation.operation_type,
                "writer_epoch": operation.writer_epoch,
                "request_digest": operation.request_digest,
            },
        )
        return DirectOperationPrepareResult(
            task=task,
            workspace=workspace,
            operation=operation,
        )


def update_prepared_operation_details(
    store: MemoryStore,
    task_id: str,
    operation_id: str,
    *,
    details: dict[str, Any],
) -> DirectWorkspaceOperation:
    now_text = _iso()
    with store._write() as conn:
        row = conn.execute(
            "SELECT * FROM direct_workspace_operations "
            "WHERE task_id = ? AND operation_id = ?",
            (task_id, operation_id),
        ).fetchone()
        if row is None:
            raise ConflictError(f"Unknown direct workspace operation: {operation_id}")
        operation = _operation_from_row(row)
        if operation.status != DirectOperationStatus.PREPARED:
            raise ConflictError(f"Direct operation is not active: {operation.status.value}")
        merged = {**operation.details, **details}
        conn.execute(
            "UPDATE direct_workspace_operations SET details_json = ?, updated_at = ? "
            "WHERE operation_id = ?",
            (_json(merged), now_text, operation_id),
        )
        return _operation_from_row(
            conn.execute(
                "SELECT * FROM direct_workspace_operations WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        )


def mark_direct_operation_reconciliation_required(
    store: MemoryStore,
    task_id: str,
    operation_id: str,
    *,
    summary: str,
    details: dict[str, Any] | None = None,
) -> DirectOperationCompleteResult:
    now_text = _iso()
    with store._write() as conn:
        task = store._task_from_row(store._task_row(conn, task_id))
        row = conn.execute(
            "SELECT * FROM direct_workspace_operations "
            "WHERE task_id = ? AND operation_id = ?",
            (task_id, operation_id),
        ).fetchone()
        if row is None:
            raise ConflictError(f"Unknown direct workspace operation: {operation_id}")
        operation = _operation_from_row(row)
        if operation.status != DirectOperationStatus.PREPARED:
            raise ConflictError(f"Direct operation is not active: {operation.status.value}")
        merged = {**operation.details, **(details or {})}
        conn.execute(
            """
            UPDATE direct_workspace_operations
            SET status = 'RECONCILIATION_REQUIRED', summary = ?, details_json = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (summary, _json(merged), now_text, operation_id),
        )
        conn.execute(
            "UPDATE task_workspace_state SET state = 'RECONCILIATION_REQUIRED', updated_at = ? "
            "WHERE task_id = ?",
            (now_text, task_id),
        )
        workspace_row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        updated_row = conn.execute(
            "SELECT * FROM direct_workspace_operations WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        return DirectOperationCompleteResult(
            task=task,
            workspace=_binding_from_row(workspace_row),
            operation=_operation_from_row(updated_row),
            reconciliation_required=True,
        )


def complete_direct_operation(
    store: MemoryStore,
    task_id: str,
    operation_id: str,
    *,
    writer_epoch: int,
    summary: str,
    git_branch: str | None,
    git_head: str | None,
    dirty: bool,
    changed_files: list[str],
    change_ref: str | None = None,
    details: dict[str, Any] | None = None,
) -> DirectOperationCompleteResult:
    now_text = _iso()
    with store._write() as conn:
        task_row = store._task_row(conn, task_id)
        current_task = store._task_from_row(task_row)
        op_row = conn.execute(
            "SELECT * FROM direct_workspace_operations "
            "WHERE task_id = ? AND operation_id = ?",
            (task_id, operation_id),
        ).fetchone()
        if op_row is None:
            raise ConflictError(f"Unknown direct workspace operation: {operation_id}")
        operation = _operation_from_row(op_row)
        if operation.status != DirectOperationStatus.PREPARED:
            raise ConflictError(f"Direct operation is not active: {operation.status.value}")
        execution = conn.execute(
            "SELECT * FROM task_execution_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        workspace_row = conn.execute(
            "SELECT * FROM task_workspace_state WHERE task_id = ?",
            (task_id,),
        ).fetchone()
        if execution is None or workspace_row is None:
            raise ConflictError("Execution/workspace state disappeared during direct operation")
        workspace = _binding_from_row(workspace_row)

        stable = (
            current_task.revision == operation.prepared_revision
            and ActiveWriter(execution["active_writer"]) == ActiveWriter.CHATGPT
            and execution["writer_epoch"] == writer_epoch == operation.writer_epoch
            and workspace.state == WorkspaceBindingStatus.ACTIVE
        )
        merged = {**operation.details, **(details or {})}
        if not stable:
            conn.execute(
                """
                UPDATE direct_workspace_operations
                SET status = 'RECONCILIATION_REQUIRED', summary = ?, change_ref = ?,
                    git_head_after = ?, details_json = ?, updated_at = ?
                WHERE operation_id = ?
                """,
                (summary, change_ref, git_head, _json(merged), now_text, operation_id),
            )
            conn.execute(
                """
                UPDATE task_workspace_state
                SET state = 'RECONCILIATION_REQUIRED', git_branch = ?, git_head = ?,
                    dirty = ?, changed_files_json = ?, last_review_ref = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (
                    git_branch,
                    git_head,
                    int(dirty),
                    _json(changed_files),
                    change_ref or workspace.last_review_ref,
                    now_text,
                    task_id,
                ),
            )
            return DirectOperationCompleteResult(
                task=current_task,
                workspace=_binding_from_row(
                    conn.execute(
                        "SELECT * FROM task_workspace_state WHERE task_id = ?",
                        (task_id,),
                    ).fetchone()
                ),
                operation=_operation_from_row(
                    conn.execute(
                        "SELECT * FROM direct_workspace_operations WHERE operation_id = ?",
                        (operation_id,),
                    ).fetchone()
                ),
                reconciliation_required=True,
            )

        task = store._update_task(
            conn,
            task_id,
            operation.prepared_revision,
            values={"git_branch": git_branch, "git_head": git_head},
        )
        conn.execute(
            """
            UPDATE direct_workspace_operations
            SET status = 'SUCCEEDED', completed_revision = ?, summary = ?, change_ref = ?,
                git_head_after = ?, details_json = ?, updated_at = ?
            WHERE operation_id = ?
            """,
            (
                task.revision,
                summary,
                change_ref,
                git_head,
                _json(merged),
                now_text,
                operation_id,
            ),
        )
        conn.execute(
            """
            UPDATE task_workspace_state
            SET git_branch = ?, git_head = ?, dirty = ?, changed_files_json = ?,
                last_review_ref = ?, state = 'ACTIVE', updated_at = ?
            WHERE task_id = ?
            """,
            (
                git_branch,
                git_head,
                int(dirty),
                _json(changed_files),
                change_ref or workspace.last_review_ref,
                now_text,
                task_id,
            ),
        )
        store._insert_event(
            conn,
            task,
            Actor.SUPERVISOR,
            EventType.STATE_RECONCILED,
            {
                "kind": "DIRECT_WORKSPACE_OPERATION_COMPLETED",
                "operation_id": operation_id,
                "operation_type": operation.operation_type,
                "writer_epoch": writer_epoch,
                "summary": summary,
                "git_head": git_head,
                "change_ref": change_ref,
                "changed_files": changed_files[:50],
            },
        )
        return DirectOperationCompleteResult(
            task=task,
            workspace=_binding_from_row(
                conn.execute(
                    "SELECT * FROM task_workspace_state WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
            ),
            operation=_operation_from_row(
                conn.execute(
                    "SELECT * FROM direct_workspace_operations WHERE operation_id = ?",
                    (operation_id,),
                ).fetchone()
            ),
        )
