from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .models import ActiveWriter, ExecutionMode, HandoffPolicy, TaskMemory, utcnow


class ExecutionState(BaseModel):
    task_id: str
    execution_mode: ExecutionMode
    active_writer: ActiveWriter
    handoff_policy: HandoffPolicy
    writer_epoch: int = Field(ge=0)
    writer_acquired_revision: int | None = Field(default=None, ge=0)
    updated_at: datetime = Field(default_factory=utcnow)


class ExecutionHandoff(BaseModel):
    handoff_id: str
    task_id: str
    from_writer: ActiveWriter
    to_writer: ActiveWriter
    from_revision: int = Field(ge=0)
    to_revision: int = Field(ge=0)
    intent_version: int = Field(ge=1)
    plan_version: int = Field(ge=0)
    writer_epoch: int = Field(ge=1)
    git_head: str | None = None
    change_ref: str | None = None
    validation: dict[str, Any] = Field(default_factory=dict)
    reason: str
    actor: str
    created_at: datetime = Field(default_factory=utcnow)


class ExecutionMutationResult(BaseModel):
    task: TaskMemory
    execution: ExecutionState
    handoff: ExecutionHandoff | None = None
