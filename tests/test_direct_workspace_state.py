from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.db.schema import (
    CHECKPOINT_MIGRATION_SQL,
    CODEX_RUNTIME_MIGRATION_SQL,
    EXECUTION_STATE_MIGRATION_SQL,
    HARD_REPLAN_MIGRATION_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    handoff_writer,
    release_writer,
    set_execution_mode,
    set_handoff_policy,
)
from codex_supervisor_bridge.memory.models import (
    ActiveWriter,
    Actor,
    ExecutionMode,
    HandoffPolicy,
)
from codex_supervisor_bridge.memory.store import MemoryStore
from codex_supervisor_bridge.memory.workspace import (
    bind_workspace,
    complete_direct_operation,
    get_prepared_direct_operation,
    get_workspace_binding,
    mark_direct_operation_reconciliation_required,
    prepare_direct_operation,
)
from codex_supervisor_bridge.memory.workspace_models import (
    DirectOperationStatus,
    WorkspaceBindingStatus,
)


def bind_and_acquire(store: MemoryStore, task_id: str = "DIRECT") -> tuple[int, int]:
    task = store.create_task(task_id, "Direct workspace", repository="C:/src/project")
    task, binding = bind_workspace(
        store,
        task.task_id,
        task.revision,
        backend_name="devspace",
        workspace_id="ws-1",
        repository="C:/src/project",
        root="C:/worktrees/ws-1",
        workspace_mode="worktree",
        base_ref="main",
        git_branch="main",
        git_head="a" * 40,
    )
    assert binding.state == WorkspaceBindingStatus.ACTIVE
    acquired = acquire_writer(
        store,
        task.task_id,
        task.revision,
        ActiveWriter.CHATGPT,
        actor=Actor.USER,
    )
    return acquired.task.revision, acquired.execution.writer_epoch


def test_v5_database_migrates_workspace_tables_forward(tmp_path: Path) -> None:
    database = tmp_path / "v5.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(CODEX_RUNTIME_MIGRATION_SQL)
        conn.executescript(CHECKPOINT_MIGRATION_SQL)
        conn.executescript(HARD_REPLAN_MIGRATION_SQL)
        conn.executescript(EXECUTION_STATE_MIGRATION_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '5') "
            "ON CONFLICT(key) DO UPDATE SET value='5'"
        )
        conn.execute(
            """
            INSERT INTO supervised_tasks(
                task_id, title, repository, status, phase, revision,
                intent_version, plan_version, created_at, updated_at
            ) VALUES ('MIGRATE-WORKSPACE', 'Migration', 'C:/src/project',
                      'active', 'created', 0, 1, 0, '2026-01-01', '2026-01-01')
            """
        )
        conn.commit()
    finally:
        conn.close()

    with MemoryStore(database) as store:
        assert current_schema_version(store._conn) == SCHEMA_VERSION
        task = store.get_task("MIGRATE-WORKSPACE")
        task, binding = bind_workspace(
            store,
            task.task_id,
            task.revision,
            backend_name="devspace",
            workspace_id="ws-migrated",
            repository="C:/src/project",
            root="C:/worktrees/migrated",
            workspace_mode="worktree",
        )
        assert task.revision == 1
        assert binding.workspace_id == "ws-migrated"

    with MemoryStore(database) as reopened:
        binding = get_workspace_binding(reopened, "MIGRATE-WORKSPACE")
        assert binding is not None
        assert binding.workspace_id == "ws-migrated"
        assert binding.state == WorkspaceBindingStatus.ACTIVE


def test_prepared_direct_operation_blocks_release_handoff_and_mode_change() -> None:
    with MemoryStore() as store:
        revision, epoch = bind_and_acquire(store)
        prepared = prepare_direct_operation(
            store,
            "DIRECT",
            revision,
            epoch,
            operation_type="APPLY_PATCH",
            request_digest="sha256:patch",
        )
        assert prepared.task.revision == revision + 1
        assert prepared.operation.status == DirectOperationStatus.PREPARED
        assert get_prepared_direct_operation(store, "DIRECT") is not None

        with pytest.raises(ConflictError, match="still in progress"):
            release_writer(
                store,
                "DIRECT",
                prepared.task.revision,
                ActiveWriter.CHATGPT,
                epoch,
            )
        with pytest.raises(ConflictError, match="still in progress"):
            handoff_writer(
                store,
                "DIRECT",
                prepared.task.revision,
                from_writer=ActiveWriter.CHATGPT,
                to_writer=ActiveWriter.CODEX,
                expected_writer_epoch=epoch,
                reason="unsafe handoff",
                actor=Actor.USER,
            )
        with pytest.raises(ConflictError, match="still in progress"):
            set_execution_mode(
                store,
                "DIRECT",
                prepared.task.revision,
                ExecutionMode.DIRECT,
            )


def test_successful_direct_operation_advances_revision_and_allows_release() -> None:
    with MemoryStore() as store:
        revision, epoch = bind_and_acquire(store)
        prepared = prepare_direct_operation(
            store,
            "DIRECT",
            revision,
            epoch,
            operation_type="APPLY_PATCH",
            request_digest="sha256:patch",
        )
        completed = complete_direct_operation(
            store,
            "DIRECT",
            prepared.operation.operation_id,
            writer_epoch=epoch,
            summary="Patched src/app.py",
            git_branch="main",
            git_head="b" * 40,
            dirty=True,
            changed_files=["src/app.py"],
            change_ref="review-1",
        )
        assert completed.reconciliation_required is False
        assert completed.operation.status == DirectOperationStatus.SUCCEEDED
        assert completed.task.revision == prepared.task.revision + 1
        assert completed.workspace.git_head == "b" * 40
        assert completed.workspace.changed_files == ["src/app.py"]
        assert completed.workspace.last_review_ref == "review-1"
        assert get_prepared_direct_operation(store, "DIRECT") is None

        released = release_writer(
            store,
            "DIRECT",
            completed.task.revision,
            ActiveWriter.CHATGPT,
            epoch,
        )
        assert released.execution.active_writer == ActiveWriter.NONE


def test_concurrent_task_revision_after_remote_write_requires_reconciliation() -> None:
    with MemoryStore() as store:
        revision, epoch = bind_and_acquire(store)
        prepared = prepare_direct_operation(
            store,
            "DIRECT",
            revision,
            epoch,
            operation_type="APPLY_PATCH",
            request_digest="sha256:patch",
        )

        # Simulate an effective user policy mutation while the already-sent
        # external patch request is in flight.
        changed = set_handoff_policy(
            store,
            "DIRECT",
            prepared.task.revision,
            HandoffPolicy.SUPERVISOR_ALLOWED,
            actor=Actor.USER,
        )
        assert changed.task.revision == prepared.task.revision + 1

        completed = complete_direct_operation(
            store,
            "DIRECT",
            prepared.operation.operation_id,
            writer_epoch=epoch,
            summary="Remote patch may have landed after state changed",
            git_branch="main",
            git_head="c" * 40,
            dirty=True,
            changed_files=["src/app.py"],
            change_ref="review-race",
        )
        assert completed.reconciliation_required is True
        assert completed.operation.status == DirectOperationStatus.RECONCILIATION_REQUIRED
        assert completed.workspace.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED
        assert completed.task.revision == changed.task.revision

        with pytest.raises(ConflictError, match="requires reconciliation"):
            release_writer(
                store,
                "DIRECT",
                changed.task.revision,
                ActiveWriter.CHATGPT,
                epoch,
            )
        with pytest.raises(ConflictError, match="requires reconciliation"):
            handoff_writer(
                store,
                "DIRECT",
                changed.task.revision,
                from_writer=ActiveWriter.CHATGPT,
                to_writer=ActiveWriter.CODEX,
                expected_writer_epoch=epoch,
                reason="must not hand off unresolved workspace",
                actor=Actor.USER,
            )


def test_explicit_reconciliation_marker_also_blocks_writer_transition() -> None:
    with MemoryStore() as store:
        revision, epoch = bind_and_acquire(store)
        prepared = prepare_direct_operation(
            store,
            "DIRECT",
            revision,
            epoch,
            operation_type="RUN_COMMAND",
            request_digest="sha256:command",
        )
        result = mark_direct_operation_reconciliation_required(
            store,
            "DIRECT",
            prepared.operation.operation_id,
            summary="Command outcome is unknown",
            details={"command_id": "17"},
        )
        assert result.reconciliation_required is True
        assert result.workspace.state == WorkspaceBindingStatus.RECONCILIATION_REQUIRED

        with pytest.raises(ConflictError, match="requires reconciliation"):
            release_writer(
                store,
                "DIRECT",
                prepared.task.revision,
                ActiveWriter.CHATGPT,
                epoch,
            )
