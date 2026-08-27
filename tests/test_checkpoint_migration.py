from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.db.schema import (
    CODEX_RUNTIME_MIGRATION_SQL,
    SCHEMA_SQL,
    SCHEMA_VERSION,
)
from codex_supervisor_bridge.memory.checkpoint_models import CheckpointType
from codex_supervisor_bridge.memory.checkpoint_store import create_checkpoint, latest_checkpoint
from codex_supervisor_bridge.memory.service import MemoryService


def test_v2_database_migrates_forward_and_persists_checkpoint(tmp_path: Path) -> None:
    database = tmp_path / "v2.db"
    conn = sqlite3.connect(database)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.executescript(CODEX_RUNTIME_MIGRATION_SQL)
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '2') "
            "ON CONFLICT(key) DO UPDATE SET value='2'"
        )
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO supervised_tasks(
                task_id, title, status, phase, revision, intent_version,
                plan_version, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("MIGRATE-CP", "Checkpoint migration", "active", "implementing", 0, 1, 0, now, now),
        )
        conn.commit()
    finally:
        conn.close()

    memory = MemoryService(database)
    try:
        assert current_schema_version(memory.store._conn) == SCHEMA_VERSION
        result = create_checkpoint(
            memory.store,
            "MIGRATE-CP",
            0,
            checkpoint_type=CheckpointType.PROGRESS,
            source_fingerprint="migration-fingerprint",
            trigger_reason="migration validation",
            completed=["checkpoint schema available"],
        )
        assert result.created is True
        assert result.task.revision == 1
    finally:
        memory.close()

    reopened = MemoryService(database)
    try:
        checkpoint = latest_checkpoint(reopened.store, "MIGRATE-CP")
        assert checkpoint is not None
        assert checkpoint.completed == ["checkpoint schema available"]
        assert checkpoint.task_revision == 1
    finally:
        reopened.close()
