from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Actor(str, Enum):
    USER = "USER"
    SUPERVISOR = "SUPERVISOR"
    CODEX = "CODEX"
    KANDEV = "KANDEV"
    GITHUB = "GITHUB"
    SYSTEM = "SYSTEM"


class TaskPhase(str, Enum):
    CREATED = "created"
    CONTEXT_READY = "context_ready"
    PLANNING = "planning"
    PLAN_REVIEW = "plan_review"
    IMPLEMENTING = "implementing"
    CHECKPOINT_PENDING = "checkpoint_pending"
    SUPERVISOR_REVIEW = "supervisor_review"
    PAUSED = "paused"
    REPLANNING = "replanning"
    CODE_REVIEW = "code_review"
    QA = "qa"
    PR = "pr"
    CI = "ci"
    FINAL_REVIEW = "final_review"
    ACCEPTED = "accepted"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ExecutionMode(str, Enum):
    DIRECT = "DIRECT"
    HYBRID = "HYBRID"
    CODEX_SUPERVISED = "CODEX_SUPERVISED"


class ActiveWriter(str, Enum):
    NONE = "NONE"
    CHATGPT = "CHATGPT"
    CODEX = "CODEX"


class HandoffPolicy(str, Enum):
    MANUAL_ONLY = "MANUAL_ONLY"
    SUPERVISOR_ALLOWED = "SUPERVISOR_ALLOWED"


class EventType(str, Enum):
    TASK_CREATED = "TASK_CREATED"
    TASK_PHASE_CHANGED = "TASK_PHASE_CHANGED"
    USER_REQUEST = "USER_REQUEST"
    USER_OVERRIDE = "USER_OVERRIDE"
    INTENT_CREATED = "INTENT_CREATED"
    INTENT_UPDATED = "INTENT_UPDATED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_APPROVED = "PLAN_APPROVED"
    PLAN_REJECTED = "PLAN_REJECTED"
    PLAN_SUPERSEDED = "PLAN_SUPERSEDED"
    DECISION_ADDED = "DECISION_ADDED"
    DECISION_SUPERSEDED = "DECISION_SUPERSEDED"
    CONSTRAINT_ADDED = "CONSTRAINT_ADDED"
    CONSTRAINT_SUPERSEDED = "CONSTRAINT_SUPERSEDED"
    SUMMARY_CREATED = "SUMMARY_CREATED"
    EVIDENCE_ADDED = "EVIDENCE_ADDED"
    CONTEXT_SNAPSHOT_CREATED = "CONTEXT_SNAPSHOT_CREATED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"
    CHECKPOINT_REVIEWED = "CHECKPOINT_REVIEWED"
    KANDEV_TASK_BOUND = "KANDEV_TASK_BOUND"
    KANDEV_SYNCED = "KANDEV_SYNCED"
    CODEX_STARTED = "CODEX_STARTED"
    CODEX_PROGRESS = "CODEX_PROGRESS"
    CODEX_STEERED = "CODEX_STEERED"
    CODEX_INTERRUPTED = "CODEX_INTERRUPTED"
    CODEX_COMPLETED = "CODEX_COMPLETED"
    TEST_STARTED = "TEST_STARTED"
    TEST_PASSED = "TEST_PASSED"
    TEST_FAILED = "TEST_FAILED"
    REVIEW_STARTED = "REVIEW_STARTED"
    REVIEW_PASSED = "REVIEW_PASSED"
    REVIEW_FAILED = "REVIEW_FAILED"
    PR_CREATED = "PR_CREATED"
    CI_PASSED = "CI_PASSED"
    CI_FAILED = "CI_FAILED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    TASK_ACCEPTED = "TASK_ACCEPTED"
    TASK_CANCELLED = "TASK_CANCELLED"
    EXECUTION_MODE_CHANGED = "EXECUTION_MODE_CHANGED"
    WRITER_ACQUIRED = "WRITER_ACQUIRED"
    WRITER_RELEASED = "WRITER_RELEASED"
    EXECUTION_HANDOFF = "EXECUTION_HANDOFF"
    EXECUTION_HANDBACK = "EXECUTION_HANDBACK"
    STATE_RECONCILED = "STATE_RECONCILED"


class DecisionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class ConstraintSeverity(str, Enum):
    HARD = "HARD"
    SOFT = "SOFT"
    PREFERENCE = "PREFERENCE"


class ConstraintStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    REVOKED = "REVOKED"


class PlanStatus(str, Enum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class SummaryType(str, Enum):
    CURRENT_STATE = "CURRENT_STATE"
    HISTORY_SEGMENT = "HISTORY_SEGMENT"
    IMPLEMENTATION = "IMPLEMENTATION"
    REVIEW = "REVIEW"
    INCIDENT = "INCIDENT"


class EvidenceType(str, Enum):
    USER_MESSAGE = "USER_MESSAGE"
    CODEX_MESSAGE = "CODEX_MESSAGE"
    CODEX_PROGRESS = "CODEX_PROGRESS"
    GIT_DIFF = "GIT_DIFF"
    FILE = "FILE"
    TEST_LOG = "TEST_LOG"
    CI_LOG = "CI_LOG"
    PLAN = "PLAN"
    REVIEW = "REVIEW"
    PR = "PR"
    COMMIT = "COMMIT"


class ContextPackMode(str, Enum):
    RESUME = "resume"
    CHECKPOINT_REVIEW = "checkpoint_review"
    PLAN_REVIEW = "plan_review"
    FINAL_REVIEW = "final_review"
    DEBUG = "debug"


class TaskMemory(BaseModel):
    task_id: str
    title: str
    repository: str | None = None
    external_kandev_task_id: str | None = None
    status: str = "active"
    phase: TaskPhase = TaskPhase.CREATED
    revision: int = Field(default=0, ge=0)
    intent_version: int = Field(default=1, ge=1)
    plan_version: int = Field(default=0, ge=0)
    current_goal: str | None = None
    current_state: str | None = None
    codex_thread_id: str | None = None
    codex_turn_id: str | None = None
    git_branch: str | None = None
    git_head: str | None = None
    pr_number: int | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TaskEvent(BaseModel):
    event_id: str
    task_id: str
    actor: Actor
    event_type: EventType
    revision: int = Field(ge=0)
    intent_version: int = Field(ge=1)
    plan_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)


class Decision(BaseModel):
    decision_id: str
    task_id: str
    decision_type: str = "general"
    status: DecisionStatus = DecisionStatus.ACTIVE
    title: str
    content: str
    source_event_id: str | None = None
    created_intent_version: int = Field(default=1, ge=1)
    superseded_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Constraint(BaseModel):
    constraint_id: str
    task_id: str
    scope: str = "task"
    severity: ConstraintSeverity = ConstraintSeverity.HARD
    status: ConstraintStatus = ConstraintStatus.ACTIVE
    content: str
    source_event_id: str | None = None
    created_revision: int = Field(default=0, ge=0)
    superseded_by: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class Plan(BaseModel):
    plan_id: str
    task_id: str
    plan_version: int = Field(ge=1)
    status: PlanStatus = PlanStatus.DRAFT
    content: str
    source_event_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class TaskSummary(BaseModel):
    summary_id: str
    task_id: str
    summary_type: SummaryType
    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=0)
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class Evidence(BaseModel):
    evidence_id: str
    task_id: str
    evidence_type: EvidenceType
    source: str
    external_id: str | None = None
    summary: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_revision: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utcnow)


class ContextSnapshot(BaseModel):
    snapshot_id: str
    task_id: str
    revision: int = Field(ge=0)
    intent_version: int = Field(ge=1)
    plan_version: int = Field(ge=0)
    mode: ContextPackMode
    token_estimate: int = Field(ge=0)
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class MemorySearchHit(BaseModel):
    kind: str
    source_id: str
    title: str
    content: str
    status: str | None = None
    score: float | None = None
