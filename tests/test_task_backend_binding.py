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
from codex_supervisor_bridge.memory.backend_binding import (
    assert_task_backend_binding,
    bind_task_backend,
    get_task_backend_binding,
)
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.service import MemoryService


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
