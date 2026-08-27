from __future__ import annotations

from typing import Any, Protocol

from .models import (
    AgentSnapshot,
    BackendHealth,
    PendingInteraction,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)


class AgentBackend(Protocol):
    async def health(self) -> BackendHealth: ...

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle: ...

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot: ...

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle: ...

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot: ...

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot: ...

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot: ...

    async def list_pending_interactions(
        self,
        handle: PlanHandle,
    ) -> list[PendingInteraction]: ...

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot: ...
