from __future__ import annotations

from pydantic import BaseModel

from codex_supervisor_bridge.backends.models import ChangeReview, CommandResult, GitState
from codex_supervisor_bridge.bootstrap.command_auth import CommandAuthorization
from codex_supervisor_bridge.memory.execution_models import ExecutionState
from codex_supervisor_bridge.memory.models import TaskMemory
from codex_supervisor_bridge.memory.workspace_models import (
    DirectCommandSession,
    DirectWorkspaceOperation,
    WorkspaceBinding,
)


class DirectWorkspaceOpenResult(BaseModel):
    task: TaskMemory
    workspace: WorkspaceBinding


class DirectWorkspaceReadResult(BaseModel):
    task_revision: int
    workspace_id: str
    path: str
    content: str


class DirectWorkspacePatchResult(BaseModel):
    task: TaskMemory
    execution: ExecutionState
    workspace: WorkspaceBinding
    operation: DirectWorkspaceOperation
    review: ChangeReview
    git: GitState
    reconciliation_required: bool = False


class DirectWorkspaceCommandResult(BaseModel):
    task: TaskMemory
    authorization: CommandAuthorization
    command: CommandResult | None = None
    operation: DirectWorkspaceOperation | None = None
    git: GitState | None = None
    review: ChangeReview | None = None
    reconciliation_required: bool = False


class DirectWorkspaceStatus(BaseModel):
    task: TaskMemory
    execution: ExecutionState
    workspace: WorkspaceBinding | None = None
    prepared_operation: DirectWorkspaceOperation | None = None
    command_session: DirectCommandSession | None = None
