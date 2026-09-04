from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.db.schema import (
    AGENT_SAFETY_MIGRATION_SQL,
    CHECKPOINT_MIGRATION_SQL,
    CODEX_RUNTIME_MIGRATION_SQL,
    EXECUTION_STATE_MIGRATION_SQL,
    HARD_REPLAN_MIGRATION_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
    WORKSPACE_STATE_MIGRATION_SQL,
)
from codex_supervisor_bridge.memory.agent_safety import latch_agent_compensation
from codex_supervisor_bridge.memory.backend_binding import (
    assert_task_backend_binding,
    bind_task_backend,
    get_task_backend_binding,
)
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import acquire_writer
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import bind_workspace, prepare_direct_operation


def test_schema_v8_persists_task_backend_binding(tmp_path: Path) -> None:
    memory = MemoryService(tmp_path / "v8.db")
    try:
        task = memory.create_task("BIND-1", "Bind runtime", repository="C:/repo")
        task, binding = bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        assert binding.workspace_backend == "devspace"
        assert binding.agent_backend == "local_codex_bridge"
        assert binding.profile == "lightweight"
        assert binding.bound_revision == task.revision
        assert binding.bound_epoch == 1

        same_revision = memory.get_task(task.task_id).revision
        unchanged = bind_task_backend(
            memory.store,
            task.task_id,
            same_revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        assert unchanged[1].bound_epoch == 1
        assert memory.get_task(task.task_id).revision == same_revision

        with pytest.raises(ConflictError, match="different backend"):
            bind_task_backend(
                memory.store,
                task.task_id,
                memory.get_task(task.task_id).revision,
                workspace_backend="kandev",
                agent_backend="control_plane",
                profile="existing",
            )
        with pytest.raises(ConflictError, match="silently switch"):
            assert_task_backend_binding(
                memory.store,
                task.task_id,
                workspace_backend="kandev",
                agent_backend="control_plane",
            )
        assert get_task_backend_binding(memory.store, task.task_id) is not None
    finally:
        memory.close()

    reopened = MemoryService(tmp_path / "v8.db")
    try:
        binding = get_task_backend_binding(reopened.store, "BIND-1")
        assert binding is not None
        assert binding.agent_backend == "local_codex_bridge"
    finally:
        reopened.close()


def test_v7_database_migrates_forward_to_v8(tmp_path: Path) -> None:
    database = tmp_path / "v7.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
        for sql in (
            CODEX_RUNTIME_MIGRATION_SQL,
            CHECKPOINT_MIGRATION_SQL,
            HARD_REPLAN_MIGRATION_SQL,
            EXECUTION_STATE_MIGRATION_SQL,
            WORKSPACE_STATE_MIGRATION_SQL,
            AGENT_SAFETY_MIGRATION_SQL,
        ):
            conn.executescript(sql)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '7') "
            "ON CONFLICT(key) DO UPDATE SET value='7'"
        )
        conn.commit()
    finally:
        conn.close()

    memory = MemoryService(database)
    try:
        assert current_schema_version(memory.store._conn) == SCHEMA_VERSION
        task = memory.create_task("MIGRATE-V8", "Migration", repository="C:/repo")
        _, binding = bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        assert binding.bound_epoch == 1
    finally:
        memory.close()


def test_explicit_controlled_migration_can_replace_binding() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-BIND", "Controlled", repository="C:/repo")
        bind_workspace(
            memory.store,
            task.task_id,
            task.revision,
            backend_name="kandev",
            workspace_id="kandev-ws",
            repository="C:/repo",
            root="C:/worktrees/kandev-ws",
            workspace_mode="checkout",
        )
        task = memory.get_task(task.task_id)
        bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="kandev",
            agent_backend="control_plane",
            profile="existing",
        )
        current = memory.get_task(task.task_id)
        _, migrated = bind_task_backend(
            memory.store,
            task.task_id,
            current.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
            allow_migration=True,
        )
        assert migrated.agent_backend == "local_codex_bridge"
        assert migrated.bound_epoch == 2
    finally:
        memory.close()


def _bind_profile_a(memory: MemoryService, task_id: str) -> int:
    task = memory.get_task(task_id)
    bind_workspace(
        memory.store,
        task.task_id,
        task.revision,
        backend_name="kandev",
        workspace_id="kandev-ws",
        repository="C:/repo",
        root="C:/worktrees/kandev-ws",
        workspace_mode="checkout",
    )
    task = memory.get_task(task_id)
    _, binding = bind_task_backend(
        memory.store,
        task.task_id,
        task.revision,
        workspace_backend="kandev",
        agent_backend="control_plane",
        profile="existing",
    )
    return binding.bound_revision


def _migrate(memory: MemoryService, task_id: str, expected_revision: int) -> None:
    bind_task_backend(
        memory.store,
        task_id,
        expected_revision,
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        profile="lightweight",
        allow_migration=True,
    )


def test_migration_requires_workspace_snapshot() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-NO-WS", "No snapshot", repository="C:/repo")
        bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="kandev",
            agent_backend="control_plane",
            profile="existing",
        )
        with pytest.raises(ConflictError, match="workspace snapshot"):
            _migrate(memory, task.task_id, memory.get_task(task.task_id).revision)
    finally:
        memory.close()


def test_migration_blocked_while_codex_owns_writer() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-CODEX", "Codex writer", repository="C:/repo")
        revision = _bind_profile_a(memory, task.task_id)
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            revision,
            ActiveWriter.CODEX,
            explicit_user_authorization=True,
        )
        with pytest.raises(ConflictError, match="CODEX is the active writer"):
            _migrate(memory, task.task_id, acquired.task.revision)
    finally:
        memory.close()


def test_migration_blocked_by_unresolved_safety_latch() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-LATCH", "Latch", repository="C:/repo")
        _bind_profile_a(memory, task.task_id)
        latched = latch_agent_compensation(
            memory.store,
            task.task_id,
            operation="execution",
            summary="compensation pending",
        )
        assert latched.state == "COMPENSATION_REQUIRED"
        with pytest.raises(ConflictError, match="compensation"):
            _migrate(memory, task.task_id, memory.get_task(task.task_id).revision)
    finally:
        memory.close()


def test_migration_blocked_by_pending_direct_operation() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-DIRECT", "Direct pending", repository="C:/repo")
        revision = _bind_profile_a(memory, task.task_id)
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            revision,
            ActiveWriter.CHATGPT,
        )
        prepared = prepare_direct_operation(
            memory.store,
            task.task_id,
            acquired.task.revision,
            acquired.execution.writer_epoch,
            operation_type="APPLY_PATCH",
            request_digest="sha256:test",
        )
        assert prepared.operation.status == "PREPARED"
        with pytest.raises(ConflictError, match="pending direct mutation"):
            _migrate(memory, task.task_id, prepared.task.revision)
    finally:
        memory.close()


def test_migration_blocked_by_active_codex_runtime() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("MIGRATE-RUNTIME", "Runtime", repository="C:/repo")
        revision = _bind_profile_a(memory, task.task_id)
        _, runtime = bind_codex_runtime(
            memory.store,
            task.task_id,
            revision,
            event_type=EventType.CODEX_STARTED,
            thread_id="thread-1",
            turn_id="turn-1",
            remote_status="executing",
        )
        assert runtime.remote_status == "executing"
        with pytest.raises(ConflictError, match="Codex runtime is active"):
            _migrate(memory, task.task_id, memory.get_task(task.task_id).revision)
    finally:
        memory.close()
