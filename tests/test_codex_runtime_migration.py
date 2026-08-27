from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codex_supervisor_bridge.db.schema import SCHEMA_SQL
from codex_supervisor_bridge.memory.codex_runtime import (
    bind_codex_runtime,
    get_codex_runtime,
)
from codex_supervisor_bridge.memory.models import EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService


def test_v1_database_migrates_to_codex_runtime_state(tmp_path: Path) -> None:
    database = tmp_path / "v1.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '1') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO supervised_tasks(
                task_id, title, status, phase, revision, intent_version,
                plan_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("MIGRATE-1", "Migration task", "active", "created", 0, 1, 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    memory = MemoryService(database)
    try:
        task = memory.get_task("MIGRATE-1")
        assert get_codex_runtime(memory.store, task.task_id) is None

        updated, runtime = bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-1",
            operation_id="op-1",
            thread_id="thread-1",
            turn_id="turn-1",
            remote_status="planning",
            next_action="wait_plan",
            client_request_id="migration-test",
            task_phase=TaskPhase.PLANNING,
        )
        assert updated.revision == 1
        assert runtime.workflow_id == "wf-1"
        assert runtime.operation_id == "op-1"
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        runtime = get_codex_runtime(reopened.store, "MIGRATE-1")
        assert runtime is not None
        assert runtime.workflow_id == "wf-1"
        assert runtime.operation_id == "op-1"
        assert runtime.thread_id == "thread-1"
        assert reopened.get_task("MIGRATE-1").phase == TaskPhase.PLANNING
    finally:
        reopened.close()
