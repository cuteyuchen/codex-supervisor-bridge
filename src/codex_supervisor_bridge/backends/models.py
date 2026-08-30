from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from codex_supervisor_bridge.memory.models import ActiveWriter


class BackendHealthStatus(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"


class BackendHealth(BaseModel):
    capability: str
    status: BackendHealthStatus
    user_message: str
    repairable: bool = False
    technical_detail: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)


class WriterLeaseToken(BaseModel):
    task_id: str
    writer: ActiveWriter
    writer_epoch: int = Field(ge=1)
    task_revision: int = Field(ge=0)


class GitState(BaseModel):
    branch: str | None = None
    head: str | None = None
    dirty: bool = False
    changed_files: list[str] = Field(default_factory=list)


class WorkspaceState(BaseModel):
    workspace_id: str
    repository: str
    root: str | None = None
    worktree: bool = True
    git: GitState = Field(default_factory=GitState)


class ChangeReview(BaseModel):
    review_ref: str | None = None
    summary: str
    files: list[str] = Field(default_factory=list)
    patch_excerpt: str | None = None


class CommandResult(BaseModel):
    command_id: str | None = None
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    truncated: bool = False


class PendingInteraction(BaseModel):
    interaction_id: str
    kind: str
    # ``kind`` remains the compact compatibility field used by the existing
    # Control Plane adapter.  The fields below make provider-neutral pending
    # approvals/questions explicit for new AgentBackend implementations.
    type: str | None = None
    summary: str | None = None
    options: list[str] = Field(default_factory=list)
    runtime_reference: dict[str, Any] = Field(default_factory=dict)
    prompt: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    runtime_instance_id: str | None = None
    runtime_epoch: int = Field(default=0, ge=0)
    runtime_ownership: str = "UNKNOWN"
    isolation_verified: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class PlanResult(BaseModel):
    """Provider-neutral result returned by a read-only planning turn."""

    content: str
    status: str = "ready"
    plan_hash: str | None = None


class AgentSnapshot(BaseModel):
    status: str
    reconciliation_required: bool = False
    plan: PlanResult | None = None
    operation_id: str | None = None
    workflow_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    runtime_instance_id: str | None = None
    runtime_epoch: int = Field(default=0, ge=0)
    runtime_ownership: str = "UNKNOWN"
    isolation_verified: bool = False
    completed: list[str] = Field(default_factory=list)
    in_progress: list[str] = Field(default_factory=list)
    files_changed: list[str] = Field(default_factory=list)
    validation: dict[str, Any] = Field(default_factory=dict)
    assumptions: list[str] = Field(default_factory=list)
    deviations: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    pending_interactions: list[PendingInteraction] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    raw_event_count: int = Field(default=0, ge=0)


class PlanHandle(BaseModel):
    task_id: str | None = None
    operation_id: str | None = None
    workflow_id: str | None = None
    thread_id: str | None = None
    turn_id: str | None = None
    runtime_instance_id: str | None = None
    runtime_epoch: int = Field(default=0, ge=0)
    runtime_ownership: str = "UNKNOWN"
    isolation_verified: bool = False
    status: str
    plan: PlanResult | None = None
    reconciliation_required: bool = False
    message: str | None = None


class DeliveryStatus(BaseModel):
    phase: str
    status: str
    pr_number: int | None = None
    commit_sha: str | None = None
    checks: dict[str, str] = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    next_action: str | None = None
