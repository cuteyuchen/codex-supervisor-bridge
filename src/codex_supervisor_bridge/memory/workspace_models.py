from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .models import TaskMemory, utcnow


class WorkspaceBindingStatus(str, Enum):
    ACTIVE = "ACTIVE"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
    CLOSED = "CLOSED"


class DirectOperationStatus(str, Enum):
    PREPARED = "PREPARED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class DirectCommandSessionStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"


class WorkspaceBinding(BaseModel):
    task_id: str
    backend_name: str
    workspace_id: str
    repository: str
    root: str | None = None
    workspace_mode: str
    base_ref: str | None = None
    git_branch: str | None = None
    git_head: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)
    last_review_ref: str | None = None
    state: WorkspaceBindingStatus = WorkspaceBindingStatus.ACTIVE
    updated_at: datetime = Field(default_factory=utcnow)


class DirectWorkspaceOperation(BaseModel):
    operation_id: str
    task_id: str
    operation_type: str
    status: DirectOperationStatus
    writer_epoch: int = Field(ge=1)
    prepared_revision: int = Field(ge=0)
    completed_revision: int | None = Field(default=None, ge=0)
    request_digest: str
    summary: str | None = None
    change_ref: str | None = None
    git_head_before: str | None = None
    git_head_after: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class DirectOperationPrepareResult(BaseModel):
    task: TaskMemory
    workspace: WorkspaceBinding
    operation: DirectWorkspaceOperation


class DirectOperationCompleteResult(BaseModel):
    task: TaskMemory
    workspace: WorkspaceBinding
    operation: DirectWorkspaceOperation
    reconciliation_required: bool = False


class DirectCommandSession(BaseModel):
    task_id: str
    command_id: str
    writer_epoch: int = Field(ge=1)
    status: DirectCommandSessionStatus
    started_revision: int = Field(ge=0)
    completed_revision: int | None = Field(default=None, ge=0)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
