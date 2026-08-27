from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .models import TaskMemory, utcnow


class CheckpointType(str, Enum):
    HEARTBEAT = "HEARTBEAT"
    PROGRESS = "PROGRESS"
    GATE = "GATE"


class CheckpointReviewDecision(str, Enum):
    CONTINUE = "CONTINUE"
    STEER = "STEER"
    INTERRUPT = "INTERRUPT"
    REPLAN = "REPLAN"
    ACCEPT = "ACCEPT"


class CodexCheckpoint(BaseModel):
    checkpoint_id: str
    task_id: str
    sequence: int = Field(ge=1)
    checkpoint_type: CheckpointType
    task_revision: int = Field(ge=0)
    intent_version: int = Field(ge=1)
    plan_version: int = Field(ge=0)
    workflow_id: str | None = None
    operation_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    remote_status: str | None = None
    next_action: str | None = None
    trigger_reason: str
    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source_fingerprint: str
    raw_event_count: int = Field(default=0, ge=0)
    requires_review: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class CheckpointReview(BaseModel):
    review_id: str
    checkpoint_id: str
    task_id: str
    decision: CheckpointReviewDecision
    instruction: str | None = None
    reviewed_revision: int = Field(ge=0)
    created_at: datetime = Field(default_factory=utcnow)


class CheckpointCreateResult(BaseModel):
    checkpoint: CodexCheckpoint
    created: bool
    task: TaskMemory
