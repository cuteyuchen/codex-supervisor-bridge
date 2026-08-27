from __future__ import annotations

SCHEMA_VERSION = 3

SCHEMA_SQL = r"""
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supervised_tasks (
    task_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    repository TEXT,
    external_kandev_task_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    phase TEXT NOT NULL DEFAULT 'created',
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0),
    intent_version INTEGER NOT NULL DEFAULT 1 CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL DEFAULT 0 CHECK (plan_version >= 0),
    current_goal TEXT,
    current_state TEXT,
    codex_thread_id TEXT,
    codex_turn_id TEXT,
    git_branch TEXT,
    git_head TEXT,
    pr_number INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    actor TEXT NOT NULL,
    event_type TEXT NOT NULL,
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_revision
ON task_events(task_id, revision, id);

CREATE INDEX IF NOT EXISTS idx_task_events_task_type
ON task_events(task_id, event_type, id);

CREATE TABLE IF NOT EXISTS task_decisions (
    decision_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    decision_type TEXT NOT NULL DEFAULT 'general',
    status TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_id TEXT,
    created_intent_version INTEGER NOT NULL CHECK (created_intent_version >= 1),
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_decisions_active
ON task_decisions(task_id, status, updated_at);

CREATE TABLE IF NOT EXISTS task_constraints (
    constraint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    scope TEXT NOT NULL DEFAULT 'task',
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_id TEXT,
    created_revision INTEGER NOT NULL CHECK (created_revision >= 0),
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_constraints_active
ON task_constraints(task_id, status, severity, updated_at);

CREATE TABLE IF NOT EXISTS task_plans (
    plan_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    plan_version INTEGER NOT NULL CHECK (plan_version >= 1),
    status TEXT NOT NULL,
    content TEXT NOT NULL,
    source_event_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, plan_version)
);

CREATE INDEX IF NOT EXISTS idx_task_plans_status
ON task_plans(task_id, status, plan_version DESC);

CREATE TABLE IF NOT EXISTS task_summaries (
    summary_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    summary_type TEXT NOT NULL,
    from_revision INTEGER NOT NULL CHECK (from_revision >= 0),
    to_revision INTEGER NOT NULL CHECK (to_revision >= from_revision),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_summaries_range
ON task_summaries(task_id, summary_type, to_revision DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS evidence_index (
    evidence_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    evidence_type TEXT NOT NULL,
    source TEXT NOT NULL,
    external_id TEXT,
    summary TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_revision INTEGER NOT NULL CHECK (created_revision >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evidence_task_revision
ON evidence_index(task_id, created_revision DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS context_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    revision INTEGER NOT NULL CHECK (revision >= 0),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    mode TEXT NOT NULL,
    token_estimate INTEGER NOT NULL CHECK (token_estimate >= 0),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_context_snapshots_task_revision
ON context_snapshots(task_id, revision DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS memory_documents (
    document_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    status TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, kind, source_id)
);

CREATE INDEX IF NOT EXISTS idx_memory_documents_task_kind
ON memory_documents(task_id, kind, updated_at DESC);
"""

CODEX_RUNTIME_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS codex_runtime_state (
    task_id TEXT PRIMARY KEY REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    workflow_id TEXT,
    operation_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    remote_status TEXT,
    next_action TEXT,
    last_client_request_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_codex_runtime_workflow
ON codex_runtime_state(workflow_id);

CREATE INDEX IF NOT EXISTS idx_codex_runtime_operation
ON codex_runtime_state(operation_id);
"""

CHECKPOINT_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS codex_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL CHECK (sequence >= 1),
    checkpoint_type TEXT NOT NULL,
    task_revision INTEGER NOT NULL CHECK (task_revision >= 0),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    workflow_id TEXT,
    operation_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    remote_status TEXT,
    next_action TEXT,
    trigger_reason TEXT NOT NULL,
    completed_json TEXT NOT NULL DEFAULT '[]',
    in_progress_json TEXT NOT NULL DEFAULT '[]',
    files_changed_json TEXT NOT NULL DEFAULT '[]',
    validation_json TEXT NOT NULL DEFAULT '{}',
    assumptions_json TEXT NOT NULL DEFAULT '[]',
    deviations_json TEXT NOT NULL DEFAULT '[]',
    blockers_json TEXT NOT NULL DEFAULT '[]',
    risks_json TEXT NOT NULL DEFAULT '[]',
    next_steps_json TEXT NOT NULL DEFAULT '[]',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    source_fingerprint TEXT NOT NULL,
    raw_event_count INTEGER NOT NULL DEFAULT 0 CHECK (raw_event_count >= 0),
    requires_review INTEGER NOT NULL DEFAULT 0 CHECK (requires_review IN (0, 1)),
    created_at TEXT NOT NULL,
    UNIQUE(task_id, sequence),
    UNIQUE(task_id, source_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_codex_checkpoints_task_sequence
ON codex_checkpoints(task_id, sequence DESC);

CREATE INDEX IF NOT EXISTS idx_codex_checkpoints_review
ON codex_checkpoints(task_id, requires_review, sequence DESC);

CREATE TABLE IF NOT EXISTS checkpoint_reviews (
    review_id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL REFERENCES codex_checkpoints(checkpoint_id) ON DELETE CASCADE,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    decision TEXT NOT NULL,
    instruction TEXT,
    reviewed_revision INTEGER NOT NULL CHECK (reviewed_revision >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(checkpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_checkpoint_reviews_task
ON checkpoint_reviews(task_id, created_at DESC);
"""

OPTIONAL_FTS_SQL = r"""
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    task_id UNINDEXED,
    kind UNINDEXED,
    source_id UNINDEXED,
    title,
    content,
    tokenize = 'unicode61'
);
"""
