from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.db.schema import (
    CHECKPOINT_MIGRATION_SQL,
    CODEX_RUNTIME_MIGRATION_SQL,
    HARD_REPLAN_MIGRATION_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from codex_supervisor_bridge.memory.errors import ConflictError, StaleRevisionError
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
    list_execution_handoffs,
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


def test_v4_database_backfills_safe_codex_supervised_execution_state(tmp_path: Path) -> None:
    database = tmp_path / "v4.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(CODEX_RUNTIME_MIGRATION_SQL)
        conn.executescript(CHECKPOINT_MIGRATION_SQL)
        conn.executescript(HARD_REPLAN_MIGRATION_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '4') "
            "ON CONFLICT(key) DO UPDATE SET value='4'"
        )
        conn.execute(
            """
            INSERT INTO supervised_tasks(
                task_id, title, status, phase, revision, intent_version,
                plan_version, created_at, updated_at
            ) VALUES ('OLD', 'Old task', 'active', 'created', 0, 1, 0, '2026-01-01', '2026-01-01')
            """
        )
        conn.commit()
    finally:
        conn.close()

    with MemoryStore(database) as store:
        assert current_schema_version(store._conn) == SCHEMA_VERSION
        state = get_execution_state(store, "OLD")
        assert state.execution_mode == ExecutionMode.CODEX_SUPERVISED
        assert state.active_writer == ActiveWriter.NONE
        assert state.handoff_policy == HandoffPolicy.MANUAL_ONLY
        assert state.writer_epoch == 0


def test_new_task_defaults_to_hybrid_manual_only() -> None:
    with MemoryStore() as store:
        task = store.create_task("NEW", "New task")
        state = get_execution_state(store, task.task_id)
        assert state.execution_mode == ExecutionMode.HYBRID
        assert state.active_writer == ActiveWriter.NONE
        assert state.handoff_policy == HandoffPolicy.MANUAL_ONLY
        assert state.writer_epoch == 0


def test_mode_change_is_revision_protected_and_cannot_invalidate_active_writer() -> None:
    with MemoryStore() as store:
        task = store.create_task("MODE", "Mode")
        direct = set_execution_mode(store, task.task_id, task.revision, ExecutionMode.DIRECT)
        assert direct.task.revision == 1
        acquired = acquire_writer(
            store,
            task.task_id,
            direct.task.revision,
            ActiveWriter.CHATGPT,
            actor=Actor.USER,
        )
        assert acquired.execution.writer_epoch == 1
        with pytest.raises(ConflictError):
            set_execution_mode(
                store,
                task.task_id,
                acquired.task.revision,
                ExecutionMode.CODEX_SUPERVISED,
            )
        with pytest.raises(StaleRevisionError):
            set_execution_mode(store, task.task_id, 0, ExecutionMode.HYBRID)


def test_single_writer_and_epoch_fence_reject_stale_release() -> None:
    with MemoryStore() as store:
        task = store.create_task("LEASE", "Lease")
        acquired = acquire_writer(
            store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
            actor=Actor.USER,
        )
        assert acquired.execution.active_writer == ActiveWriter.CHATGPT
        assert acquired.execution.writer_epoch == 1
        with pytest.raises(ConflictError):
            acquire_writer(
                store,
                task.task_id,
                acquired.task.revision,
                ActiveWriter.CODEX,
                actor=Actor.USER,
            )
        with pytest.raises(ConflictError):
            release_writer(
                store,
                task.task_id,
                acquired.task.revision,
                ActiveWriter.CHATGPT,
                expected_writer_epoch=0,
            )
        released = release_writer(
            store,
            task.task_id,
            acquired.task.revision,
            ActiveWriter.CHATGPT,
            expected_writer_epoch=1,
        )
        assert released.execution.active_writer == ActiveWriter.NONE
        assert released.execution.writer_epoch == 1


def test_hybrid_manual_policy_blocks_autonomous_codex_handoff() -> None:
    with MemoryStore() as store:
        task = store.create_task("HYBRID", "Hybrid")
        chatgpt = acquire_writer(
            store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
            actor=Actor.USER,
        )
        with pytest.raises(ConflictError):
            handoff_writer(
                store,
                task.task_id,
                chatgpt.task.revision,
                from_writer=ActiveWriter.CHATGPT,
                to_writer=ActiveWriter.CODEX,
                expected_writer_epoch=chatgpt.execution.writer_epoch,
                reason="Delegate complex refactor",
                actor=Actor.SUPERVISOR,
            )
        handed = handoff_writer(
            store,
            task.task_id,
            chatgpt.task.revision,
            from_writer=ActiveWriter.CHATGPT,
            to_writer=ActiveWriter.CODEX,
            expected_writer_epoch=chatgpt.execution.writer_epoch,
            reason="User explicitly asked Codex to take this part",
            git_head="abc123",
            change_ref="review:1",
            validation={"pytest": "passed"},
            actor=Actor.USER,
        )
        assert handed.execution.active_writer == ActiveWriter.CODEX
        assert handed.execution.writer_epoch == 2
        assert handed.handoff is not None
        assert handed.handoff.change_ref == "review:1"
        history = list_execution_handoffs(store, task.task_id)
        assert history[0].validation == {"pytest": "passed"}


def test_user_can_allow_supervisor_codex_delegation_and_handback() -> None:
    with MemoryStore() as store:
        task = store.create_task("AUTO", "Automatic hybrid")
        policy = set_handoff_policy(
            store,
            task.task_id,
            task.revision,
            HandoffPolicy.SUPERVISOR_ALLOWED,
            actor=Actor.USER,
        )
        chatgpt = acquire_writer(
            store,
            task.task_id,
            policy.task.revision,
            ActiveWriter.CHATGPT,
            actor=Actor.SUPERVISOR,
        )
        codex = handoff_writer(
            store,
            task.task_id,
            chatgpt.task.revision,
            from_writer=ActiveWriter.CHATGPT,
            to_writer=ActiveWriter.CODEX,
            expected_writer_epoch=chatgpt.execution.writer_epoch,
            reason="Supervisor selected Codex for a bounded implementation",
            actor=Actor.SUPERVISOR,
        )
        assert codex.execution.active_writer == ActiveWriter.CODEX
        handback = handoff_writer(
            store,
            task.task_id,
            codex.task.revision,
            from_writer=ActiveWriter.CODEX,
            to_writer=ActiveWriter.CHATGPT,
            expected_writer_epoch=codex.execution.writer_epoch,
            reason="Codex completed the bounded work",
            actor=Actor.SUPERVISOR,
        )
        assert handback.execution.active_writer == ActiveWriter.CHATGPT
        assert handback.execution.writer_epoch == 3


def test_only_user_can_enable_automatic_delegation() -> None:
    with MemoryStore() as store:
        task = store.create_task("POLICY", "Policy")
        with pytest.raises(ConflictError):
            set_handoff_policy(
                store,
                task.task_id,
                task.revision,
                HandoffPolicy.SUPERVISOR_ALLOWED,
                actor=Actor.SUPERVISOR,
            )
