from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .models import TaskMemory, utcnow


class SnapshotClassificationStatus(str, Enum):
    UNCLASSIFIED = "UNCLASSIFIED"
    CLASSIFIED = "CLASSIFIED"


class HardReplanStatus(str, Enum):
    INTERRUPT_PENDING = "INTERRUPT_PENDING"
    SNAPSHOT_READY = "SNAPSHOT_READY"
    INTERRUPT_FAILED = "INTERRUPT_FAILED"
    READY_TO_PLAN = "READY_TO_PLAN"
    PLANNING = "PLANNING"
    PLAN_REVIEW = "PLAN_REVIEW"
    COMPLETED = "COMPLETED"
    SUPERSEDED = "SUPERSEDED"


class WorkSnapshot(BaseModel):
    snapshot_id: str
    task_id: str
    captured_revision: int = Field(ge=0)
    intent_version: int = Field(ge=1)
    plan_version: int = Field(ge=0)
    goal: str | None = None
    phase: str
    approved_plan_id: str | None = None
    kandev_task_id: str | None = None
    git_branch: str | None = None
    git_head: str | None = None
    checkpoint_id: str | None = None
    codex_workflow_id: str | None = None
    operation_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    remote_status: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    keep: list[str] = Field(default_factory=list)
    modify: list[str] = Field(default_factory=list)
    drop: list[str] = Field(default_factory=list)
    classification_notes: str | None = None
    classification_status: SnapshotClassificationStatus = SnapshotClassificationStatus.UNCLASSIFIED
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class HardReplan(BaseModel):
    replan_id: str
    task_id: str
    snapshot_id: str
    status: HardReplanStatus
    from_intent_version: int = Field(ge=1)
    target_intent_version: int = Field(ge=1)
    previous_plan_id: str | None = None
    new_plan_id: str | None = None
    new_goal: str
    reason: str
    interrupt_error: str | None = None
    new_workflow_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class HardReplanBeginResult(BaseModel):
    task: TaskMemory
    snapshot: WorkSnapshot
    replan: HardReplan
