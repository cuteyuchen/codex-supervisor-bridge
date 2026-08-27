from __future__ import annotations

SCHEMA_VERSION = 7

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

HARD_REPLAN_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS work_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    captured_revision INTEGER NOT NULL CHECK (captured_revision >= 0),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    goal TEXT,
    phase TEXT NOT NULL,
    approved_plan_id TEXT,
    kandev_task_id TEXT,
    git_branch TEXT,
    git_head TEXT,
    checkpoint_id TEXT,
    codex_workflow_id TEXT,
    operation_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    remote_status TEXT,
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    validation_json TEXT NOT NULL DEFAULT '{}',
    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
    keep_json TEXT NOT NULL DEFAULT '[]',
    modify_json TEXT NOT NULL DEFAULT '[]',
    drop_json TEXT NOT NULL DEFAULT '[]',
    classification_notes TEXT,
    classification_status TEXT NOT NULL DEFAULT 'UNCLASSIFIED',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_work_snapshots_task_revision
ON work_snapshots(task_id, captured_revision DESC, created_at DESC);

CREATE TABLE IF NOT EXISTS hard_replans (
    replan_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    snapshot_id TEXT NOT NULL REFERENCES work_snapshots(snapshot_id) ON DELETE CASCADE,
    status TEXT NOT NULL,
    from_intent_version INTEGER NOT NULL CHECK (from_intent_version >= 1),
    target_intent_version INTEGER NOT NULL CHECK (target_intent_version >= 1),
    previous_plan_id TEXT,
    new_plan_id TEXT,
    new_goal TEXT NOT NULL,
    reason TEXT NOT NULL,
    interrupt_error TEXT,
    new_workflow_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_hard_replans_one_active
ON hard_replans(task_id)
WHERE status IN (
    'INTERRUPT_PENDING', 'SNAPSHOT_READY', 'INTERRUPT_FAILED',
    'READY_TO_PLAN', 'PLANNING', 'PLAN_REVIEW'
);

CREATE INDEX IF NOT EXISTS idx_hard_replans_task_created
ON hard_replans(task_id, created_at DESC);
"""

EXECUTION_STATE_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS task_execution_state (
    task_id TEXT PRIMARY KEY REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    execution_mode TEXT NOT NULL DEFAULT 'HYBRID'
        CHECK (execution_mode IN ('DIRECT', 'HYBRID', 'CODEX_SUPERVISED')),
    active_writer TEXT NOT NULL DEFAULT 'NONE'
        CHECK (active_writer IN ('NONE', 'CHATGPT', 'CODEX')),
    handoff_policy TEXT NOT NULL DEFAULT 'MANUAL_ONLY'
        CHECK (handoff_policy IN ('MANUAL_ONLY', 'SUPERVISOR_ALLOWED')),
    writer_epoch INTEGER NOT NULL DEFAULT 0 CHECK (writer_epoch >= 0),
    writer_acquired_revision INTEGER CHECK (writer_acquired_revision >= 0),
    updated_at TEXT NOT NULL
);

INSERT OR IGNORE INTO task_execution_state(
    task_id, execution_mode, active_writer, handoff_policy,
    writer_epoch, writer_acquired_revision, updated_at
)
SELECT
    task_id, 'CODEX_SUPERVISED', 'NONE', 'MANUAL_ONLY',
    0, NULL, updated_at
FROM supervised_tasks;

CREATE TRIGGER IF NOT EXISTS trg_supervised_task_execution_state
AFTER INSERT ON supervised_tasks
BEGIN
    INSERT OR IGNORE INTO task_execution_state(
        task_id, execution_mode, active_writer, handoff_policy,
        writer_epoch, writer_acquired_revision, updated_at
    ) VALUES (
        NEW.task_id, 'HYBRID', 'NONE', 'MANUAL_ONLY',
        0, NULL, NEW.created_at
    );
END;

CREATE TABLE IF NOT EXISTS execution_handoffs (
    handoff_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    from_writer TEXT NOT NULL CHECK (from_writer IN ('CHATGPT', 'CODEX')),
    to_writer TEXT NOT NULL CHECK (to_writer IN ('CHATGPT', 'CODEX')),
    from_revision INTEGER NOT NULL CHECK (from_revision >= 0),
    to_revision INTEGER NOT NULL CHECK (to_revision >= 0),
    intent_version INTEGER NOT NULL CHECK (intent_version >= 1),
    plan_version INTEGER NOT NULL CHECK (plan_version >= 0),
    writer_epoch INTEGER NOT NULL CHECK (writer_epoch >= 1),
    git_head TEXT,
    change_ref TEXT,
    validation_json TEXT NOT NULL DEFAULT '{}',
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_execution_handoffs_task_created
ON execution_handoffs(task_id, created_at DESC);
"""

WORKSPACE_STATE_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS task_workspace_state (
    task_id TEXT PRIMARY KEY REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    backend_name TEXT NOT NULL,
    workspace_id TEXT NOT NULL,
    repository TEXT NOT NULL,
    root TEXT,
    workspace_mode TEXT NOT NULL CHECK (workspace_mode IN ('checkout', 'worktree')),
    base_ref TEXT,
    git_branch TEXT,
    git_head TEXT,
    dirty INTEGER NOT NULL DEFAULT 0 CHECK (dirty IN (0, 1)),
    changed_files_json TEXT NOT NULL DEFAULT '[]',
    last_review_ref TEXT,
    state TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK (state IN ('ACTIVE', 'RECONCILIATION_REQUIRED', 'CLOSED')),
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_workspace_backend
ON task_workspace_state(backend_name, workspace_id);

CREATE TABLE IF NOT EXISTS direct_workspace_operations (
    operation_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('PREPARED', 'SUCCEEDED', 'FAILED', 'RECONCILIATION_REQUIRED')),
    writer_epoch INTEGER NOT NULL CHECK (writer_epoch >= 1),
    prepared_revision INTEGER NOT NULL CHECK (prepared_revision >= 0),
    completed_revision INTEGER CHECK (completed_revision >= 0),
    request_digest TEXT NOT NULL,
    summary TEXT,
    change_ref TEXT,
    git_head_before TEXT,
    git_head_after TEXT,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_direct_workspace_one_prepared
ON direct_workspace_operations(task_id)
WHERE status = 'PREPARED';

CREATE INDEX IF NOT EXISTS idx_direct_workspace_task_created
ON direct_workspace_operations(task_id, created_at DESC);

CREATE TABLE IF NOT EXISTS direct_command_sessions (
    task_id TEXT NOT NULL REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    command_id TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL CHECK (writer_epoch >= 1),
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'COMPLETED', 'INTERRUPTED', 'UNKNOWN')),
    started_revision INTEGER NOT NULL CHECK (started_revision >= 0),
    completed_revision INTEGER CHECK (completed_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY(task_id, command_id)
);

CREATE INDEX IF NOT EXISTS idx_direct_command_running
ON direct_command_sessions(task_id, status, created_at DESC);
"""

AGENT_SAFETY_MIGRATION_SQL = r"""
CREATE TABLE IF NOT EXISTS task_agent_safety (
    task_id TEXT PRIMARY KEY REFERENCES supervised_tasks(task_id) ON DELETE CASCADE,
    state TEXT NOT NULL DEFAULT 'NONE'
        CHECK (state IN ('NONE', 'COMPENSATION_REQUIRED', 'RECONCILIATION_REQUIRED')),
    operation TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    workflow_id TEXT,
    operation_id TEXT,
    thread_id TEXT,
    turn_id TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_agent_safety_state
ON task_agent_safety(state, updated_at DESC);
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
