from __future__ import annotations

from typing import Protocol

from .models import (
    BackendHealth,
    ChangeReview,
    CommandResult,
    GitState,
    WorkspaceState,
    WriterLeaseToken,
)


class WorkspaceBackend(Protocol):
    async def health(self) -> BackendHealth: ...

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState: ...

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str: ...

    async def apply_patch(
        self,
        workspace_id: str,
        patch: str,
        *,
        lease: WriterLeaseToken,
    ) -> ChangeReview: ...

    async def run_command(
        self,
        workspace_id: str,
        command: str,
        *,
        lease: WriterLeaseToken,
        yield_time_ms: int = 10_000,
        tty: bool = False,
    ) -> CommandResult: ...

    async def poll_command(
        self,
        workspace_id: str,
        command_id: str,
        *,
        input_text: str | None = None,
        interrupt: bool = False,
    ) -> CommandResult: ...

    async def show_changes(self, workspace_id: str) -> ChangeReview: ...

    async def git_state(self, workspace_id: str) -> GitState: ...

    async def close_workspace(self, workspace_id: str) -> None: ...
