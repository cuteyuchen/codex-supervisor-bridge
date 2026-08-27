from __future__ import annotations

from typing import Protocol

from .models import BackendHealth, DeliveryStatus, WorkspaceState, WriterLeaseToken


class DeliveryBackend(Protocol):
    async def health(self) -> BackendHealth: ...

    async def review(
        self,
        *,
        task_id: str,
        workspace: WorkspaceState,
    ) -> DeliveryStatus: ...

    async def qa(
        self,
        *,
        task_id: str,
        workspace: WorkspaceState,
    ) -> DeliveryStatus: ...

    async def commit(
        self,
        *,
        task_id: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
        message: str,
    ) -> DeliveryStatus: ...

    async def push(
        self,
        *,
        task_id: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> DeliveryStatus: ...

    async def create_draft_pr(
        self,
        *,
        task_id: str,
        workspace: WorkspaceState,
        title: str,
        body: str,
    ) -> DeliveryStatus: ...

    async def get_ci_status(self, *, task_id: str, pr_number: int) -> DeliveryStatus: ...

    async def get_ci_failure_detail(
        self,
        *,
        task_id: str,
        pr_number: int,
    ) -> DeliveryStatus: ...
