from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from codex_supervisor_bridge.backends.agent import AgentBackend
from codex_supervisor_bridge.backends.models import (
    BackendHealth,
    BackendHealthStatus,
    PendingInteraction,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import (
    AgentSafetyState,
    get_agent_safety,
    record_agent_compensation_required,
)
from codex_supervisor_bridge.memory.backend_binding import (
    TaskBackendBinding,
    get_task_backend_binding,
    list_runtime_affinity_task_ids,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeState,
    get_codex_runtime,
    is_active_runtime,
    is_execution_runtime,
)
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import get_execution_state
from codex_supervisor_bridge.memory.models import ActiveWriter
from codex_supervisor_bridge.memory.service import MemoryService


class AgentSessionUnavailableError(RuntimeError):
    """The long-lived AgentBackend session is not connected."""


class SessionRecoveryOutcome(BaseModel):
    task_id: str
    status: str
    detail: str


class AgentSessionManager:
    """Own one long-lived client-owned AgentBackend session for the Supervisor.

    Stdio MCP agents (Local-Codex-Bridge) must not be started as detached
    daemons and then re-created per tool call. The manager keeps one session
    alive, reuses it for the full turn lifecycle, and applies fail-closed
    recovery rules when the Supervisor process restarts.
    """

    def __init__(
        self,
        memory: MemoryService,
        backend_factory: Callable[[], AgentBackend],
        *,
        profile: str = "unavailable",
        workspace_backend: str = "unknown",
        agent_backend: str = "unknown",
    ) -> None:
        self.memory = memory
        self._backend_factory = backend_factory
        self._backend: AgentBackend | None = None
        self.profile = profile
        self.workspace_backend = workspace_backend
        self.agent_backend = agent_backend
        self.session_count = 0
        self.shutdown_count = 0
        self.reconnect_count = 0
        self.recovery_outcomes: list[SessionRecoveryOutcome] = []
        self._lock = asyncio.Lock()

    @property
    def backend(self) -> AgentBackend:
        if self._backend is None:
            raise AgentSessionUnavailableError("Agent session is not connected")
        return self._backend

    @property
    def connected(self) -> bool:
        return self._backend is not None

    async def _ensure_started(self) -> None:
        if self._backend is None:
            await self.start()

    async def start(self) -> list[SessionRecoveryOutcome]:
        async with self._lock:
            if self._backend is not None:
                return list(self.recovery_outcomes)
            backend = self._backend_factory()
            enter = getattr(backend, "__aenter__", None)
            if enter is not None:
                await enter()
            self._backend = backend
            self.session_count += 1
            self.recovery_outcomes = await self._recover_active_runtimes()
            return list(self.recovery_outcomes)

    async def shutdown(self) -> None:
        async with self._lock:
            backend = self._backend
            self._backend = None
            if backend is not None:
                exit_method = getattr(backend, "__aexit__", None)
                if exit_method is not None:
                    try:
                        await exit_method(None, None, None)
                    finally:
                        self.shutdown_count += 1
                else:
                    self.shutdown_count += 1

    async def reconnect(self) -> None:
        await self.shutdown()
        async with self._lock:
            self.reconnect_count += 1
        await self.start()

    async def health(self) -> BackendHealth:
        await self._ensure_started()
        backend = self._backend
        if backend is None:
            return BackendHealth(
                capability=self.agent_backend,
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex control is not connected.",
                repairable=True,
                technical_detail="agent session is not started",
            )
        try:
            return await backend.health()
        except Exception as exc:
            return BackendHealth(
                capability=self.agent_backend,
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex control is not ready.",
                repairable=True,
                technical_detail=f"health probe failed: {type(exc).__name__}",
            )

    async def probe_health(self) -> BackendHealth:
        """Bounded ephemeral protocol probe without holding the live session."""
        backend = self._backend_factory()
        enter = getattr(backend, "__aenter__", None)
        if enter is not None:
            await enter()
            try:
                return await backend.health()
            finally:
                await backend.__aexit__(None, None, None)
        return await backend.health()

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        await self._ensure_started()
        return await self.backend.start_plan(
            task_id=task_id,
            context_pack=context_pack,
            workspace=workspace,
        )

    async def get_plan_status(self, handle: PlanHandle) -> Any:
        await self._ensure_started()
        return await self.backend.get_plan_status(handle)

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        await self._ensure_started()
        return await self.backend.start_execution(
            task_id=task_id,
            context_pack=context_pack,
            approved_plan=approved_plan,
            workspace=workspace,
            lease=lease,
        )

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> Any:
        await self._ensure_started()
        return await self.backend.observe(handle, cursor=cursor, wait_ms=wait_ms)

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> Any:
        await self._ensure_started()
        return await self.backend.steer(handle, instruction, lease=lease)

    async def interrupt(self, handle: PlanHandle) -> Any:
        await self._ensure_started()
        return await self.backend.interrupt(handle)

    async def list_pending_interactions(
        self,
        handle: PlanHandle,
    ) -> list[PendingInteraction]:
        await self._ensure_started()
        return await self.backend.list_pending_interactions(handle)

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> Any:
        await self._ensure_started()
        return await self.backend.respond_interaction(handle, interaction, response)

    async def resume(self, handle: PlanHandle) -> Any:
        await self._ensure_started()
        return await self.backend.resume(handle)

    def _task_ids_requiring_runtime_recovery(self) -> list[str]:
        return list_runtime_affinity_task_ids(self.memory.store)

    def _handle_for_runtime(self, runtime: CodexRuntimeState) -> PlanHandle:
        return PlanHandle(
            operation_id=runtime.operation_id,
            workflow_id=runtime.workflow_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            status=runtime.remote_status or "unknown",
        )

    async def _recover_active_runtimes(self) -> list[SessionRecoveryOutcome]:
        backend = self._backend
        if backend is None:
            return []
        outcomes: list[SessionRecoveryOutcome] = []
        for task_id in self._task_ids_requiring_runtime_recovery():
            binding = get_task_backend_binding(self.memory.store, task_id)
            if not self._binding_matches(binding):
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail=(
                            "task is bound to a different backend profile; "
                            "this composition cannot resume its active runtime"
                        ),
                    )
                )
                continue
            try:
                runtime = get_codex_runtime(self.memory.store, task_id)
                execution = get_execution_state(self.memory.store, task_id)
                safety = get_agent_safety(self.memory.store, task_id)
            except ConflictError:
                continue
            if safety is not None and safety.state != AgentSafetyState.NONE.value:
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail=(
                            "an unresolved agent compensation/reconciliation "
                            "latch blocks runtime recovery"
                        ),
                    )
                )
                continue
            if not is_active_runtime(runtime.remote_status if runtime else None):
                continue
            if (
                is_execution_runtime(runtime.remote_status)
                and not (
                    execution.active_writer == ActiveWriter.CODEX
                    and execution.writer_epoch >= 1
                )
            ):
                await self._latch_reconciliation(
                    task_id,
                    runtime,
                    "workspace-write runtime requires an active CODEX writer lease",
                )
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail=(
                            "execution runtime is not owned by a current "
                            "CODEX writer lease"
                        ),
                    )
                )
                continue
            if runtime is None or not any(
                [runtime.workflow_id, runtime.operation_id, runtime.thread_id, runtime.turn_id]
            ):
                await self._latch_reconciliation(task_id, runtime, "persisted Codex runtime identity is missing")
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail="persisted Codex runtime identity is missing",
                    )
                )
                continue
            handle = self._handle_for_runtime(runtime)
            try:
                snapshot = await backend.resume(handle)
            except Exception as exc:
                await self._latch_reconciliation(
                    task_id,
                    runtime,
                    f"resume/observe failed: {type(exc).__name__}",
                )
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail=f"resume/observe failed: {type(exc).__name__}",
                    )
                )
                continue
            status = (snapshot.status or "").strip().lower()
            if snapshot.reconciliation_required or status in {
                "unknown",
                "reconciliation_required",
                "compensation_required",
            }:
                await self._latch_reconciliation(
                    task_id,
                    runtime,
                    f"resume/observe returned {snapshot.status!r}",
                )
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RECONCILIATION_REQUIRED",
                        detail=f"resume/observe returned {snapshot.status!r}",
                    )
                )
                continue
            outcomes.append(
                SessionRecoveryOutcome(
                    task_id=task_id,
                    status="RESUMED",
                    detail=f"runtime confirmed through thread={handle.thread_id} turn={handle.turn_id}",
                )
            )
        return outcomes

    def _binding_matches(self, binding: TaskBackendBinding | None) -> bool:
        if binding is None:
            return True
        return (
            binding.workspace_backend == self.workspace_backend
            and binding.agent_backend == self.agent_backend
            and binding.profile == self.profile
        )

    async def _latch_reconciliation(
        self,
        task_id: str,
        runtime: CodexRuntimeState | None,
        reason: str,
    ) -> None:
        try:
            record_agent_compensation_required(
                self.memory.store,
                task_id,
                operation="runtime_recovery",
                summary=(
                    "Codex runtime state cannot be safely resumed after a restart; "
                    "reconciliation is required before new Codex work."
                ),
                details={"recovery_reason": reason},
                workflow_id=runtime.workflow_id if runtime else None,
                operation_id=runtime.operation_id if runtime else None,
                thread_id=runtime.thread_id if runtime else None,
                turn_id=runtime.turn_id if runtime else None,
            )
        except ConflictError:
            # The task already carries a compensation/reconciliation latch.
            pass

    def status(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "workspace_backend": self.workspace_backend,
            "agent_backend": self.agent_backend,
            "connected": self.connected,
            "session_count": self.session_count,
            "shutdown_count": self.shutdown_count,
            "reconnect_count": self.reconnect_count,
            "recovery_outcomes": [outcome.model_dump(mode="json") for outcome in self.recovery_outcomes],
        }
