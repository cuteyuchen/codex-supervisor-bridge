from __future__ import annotations

import asyncio
import logging
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
from codex_supervisor_bridge.bootstrap.codex_isolation import (
    CodexRuntimeIsolationError,
    SupervisorCodexRuntimeManager,
)
from codex_supervisor_bridge.memory.agent_safety import (
    AgentSafetyState,
    get_agent_safety,
    record_agent_compensation_required,
    record_agent_compensation_succeeded,
)
from codex_supervisor_bridge.memory.backend_binding import (
    TaskBackendBinding,
    get_task_backend_binding,
    list_runtime_affinity_task_ids,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeAffinityError,
    CodexRuntimeCircuitOpenError,
    CodexRuntimeState,
    assert_runtime_affinity,
    assert_runtime_circuit_closed,
    bind_codex_runtime,
    close_runtime_circuit_after_recovery,
    get_codex_runtime,
    is_active_runtime,
    is_execution_runtime,
    is_plan_runtime,
    mark_protocol_interrupt_attempted,
    open_runtime_circuit,
    record_runtime_observation,
)
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import get_execution_state
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService

logger = logging.getLogger(__name__)


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
        runtime_manager: SupervisorCodexRuntimeManager | None = None,
    ) -> None:
        self.memory = memory
        self._backend_factory = backend_factory
        self._backend: AgentBackend | None = None
        self.profile = profile
        self.workspace_backend = workspace_backend
        self.agent_backend = agent_backend
        self.runtime_manager = runtime_manager
        self.session_count = 0
        self.shutdown_count = 0
        self.reconnect_count = 0
        self.recovery_outcomes: list[SessionRecoveryOutcome] = []
        self.runtime_error: str | None = None
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
        if self._backend is None:
            raise AgentSessionUnavailableError(
                self.runtime_error or "Agent session is not connected"
            )

    async def start(self) -> list[SessionRecoveryOutcome]:
        async with self._lock:
            if self._backend is not None:
                return list(self.recovery_outcomes)
            if self.runtime_error is not None:
                return list(self.recovery_outcomes)
            backend = self._backend_factory()
            enter = getattr(backend, "__aenter__", None)
            entered = False
            try:
                if enter is not None:
                    await enter()
                    entered = True
                if self.runtime_manager is not None:
                    self.runtime_manager.wait_until_verified()
            except Exception as exc:
                if entered:
                    exit_method = getattr(backend, "__aexit__", None)
                    if exit_method is not None:
                        try:
                            await exit_method(None, None, None)
                        except Exception:
                            pass
                if self.runtime_manager is not None:
                    self.runtime_manager.mark_degraded(
                        "SUPERVISOR_CODEX_RUNTIME_FAILED",
                        type(exc).__name__,
                    )
                self.runtime_error = (
                    "SUPERVISOR_CODEX_RUNTIME_FAILED: isolated Codex runtime "
                    "ownership could not be verified"
                )
                return list(self.recovery_outcomes)
            self._backend = backend
            self.session_count += 1
            self.runtime_error = None
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
                        if self.runtime_manager is not None:
                            try:
                                self.runtime_manager.mark_stopped()
                            except CodexRuntimeIsolationError:
                                pass
                else:
                    self.shutdown_count += 1
                    if self.runtime_manager is not None:
                        try:
                            self.runtime_manager.mark_stopped()
                        except CodexRuntimeIsolationError:
                            pass

    async def reconnect(self) -> None:
        if self.runtime_manager is not None:
            self.runtime_manager.assert_destructive_lifecycle_allowed()
        await self.shutdown()
        async with self._lock:
            self.reconnect_count += 1
            self.runtime_error = None
            if self.runtime_manager is not None:
                self.runtime_manager.replace()
        await self.start()

    async def recover_runtime(self, task_id: str) -> CodexRuntimeState:
        runtime = self._required_runtime(task_id)
        if not runtime.circuit_open:
            raise CodexRuntimeCircuitOpenError(
                "Codex runtime recovery is allowed only while the circuit is open"
            )
        if self.runtime_manager is None:
            raise CodexRuntimeAffinityError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: no Supervisor runtime manager"
            )
        await self.reconnect()
        if self._backend is None:
            raise AgentSessionUnavailableError(
                self.runtime_error or "SUPERVISOR_CODEX_RUNTIME_FAILED"
            )
        metadata = self.runtime_manager.refresh()
        if metadata.status != "READY" or not metadata.isolation_verified:
            raise CodexRuntimeAffinityError(
                "SUPERVISOR_CODEX_RUNTIME_FAILED: replacement runtime is not verified"
            )
        recovered = close_runtime_circuit_after_recovery(
            self.memory.store,
            task_id,
            runtime_instance_id=metadata.instance_id,
            runtime_epoch=metadata.runtime_epoch,
            ownership=metadata.ownership.value,
            isolation_verified=metadata.isolation_verified,
            runtime_status=metadata.status,
        )
        self.recovery_outcomes = [
            outcome for outcome in self.recovery_outcomes if outcome.task_id != task_id
        ]
        logger.info(
            "runtime recovery completed task_id=%s instance_id=%s epoch=%s",
            task_id,
            metadata.instance_id,
            metadata.runtime_epoch,
        )
        return recovered

    async def health(self) -> BackendHealth:
        if self.runtime_error is None:
            try:
                await self._ensure_started()
            except AgentSessionUnavailableError:
                pass
        backend = self._backend
        if backend is None:
            return BackendHealth(
                capability=self.agent_backend,
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex control is not connected.",
                repairable=True,
                technical_detail=self.runtime_error or "agent session is not started",
            )
        if self.runtime_manager is not None:
            try:
                metadata = self.runtime_manager.refresh()
            except CodexRuntimeIsolationError:
                metadata = None
            if metadata is None or not metadata.isolation_verified:
                return BackendHealth(
                    capability=self.agent_backend,
                    status=BackendHealthStatus.UNAVAILABLE,
                    user_message="Codex needs runtime recovery.",
                    repairable=True,
                    technical_detail="UNSAFE_SHARED_CODEX_RUNTIME or ownership unknown",
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
        if self.runtime_manager is not None:
            existing = get_codex_runtime(self.memory.store, task_id)
            if existing is not None:
                assert_runtime_circuit_closed(existing)
        handle = await self.backend.start_plan(
            task_id=task_id,
            context_pack=context_pack,
            workspace=workspace,
        )
        stamped = self._stamp_handle(handle, task_id=task_id)
        logger.info(
            "turn started task_id=%s mode=plan instance_id=%s epoch=%s",
            task_id,
            stamped.runtime_instance_id,
            stamped.runtime_epoch,
        )
        return stamped

    async def get_plan_status(self, handle: PlanHandle) -> Any:
        return await self.observe(handle)

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
        if self.runtime_manager is not None:
            self._assert_handle_runtime(handle=None, task_id=task_id, require_existing=True)
        handle = await self.backend.start_execution(
            task_id=task_id,
            context_pack=context_pack,
            approved_plan=approved_plan,
            workspace=workspace,
            lease=lease,
        )
        stamped = self._stamp_handle(handle, task_id=task_id)
        logger.info(
            "turn started task_id=%s mode=execute instance_id=%s epoch=%s",
            task_id,
            stamped.runtime_instance_id,
            stamped.runtime_epoch,
        )
        return stamped

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> Any:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        try:
            snapshot = await self.backend.observe(handle, cursor=cursor, wait_ms=wait_ms)
        except Exception as exc:
            if handle.task_id:
                open_runtime_circuit(
                    self.memory.store,
                    handle.task_id,
                    reason="CODEX_APP_SERVER_DISCONNECTED",
                    remote_status="CODEX_APP_SERVER_DISCONNECTED",
                )
            if self.runtime_manager is not None:
                self.runtime_manager.mark_degraded(
                    "CODEX_APP_SERVER_DISCONNECTED",
                    type(exc).__name__,
                )
            raise
        snapshot = self._stamp_snapshot(snapshot, handle)
        if handle.task_id and self.runtime_manager is not None:
            observed_runtime = record_runtime_observation(
                self.memory.store,
                handle.task_id,
                snapshot,
            )
            if observed_runtime.remote_status == "CODEX_TURN_STALLED":
                snapshot = snapshot.model_copy(
                    update={
                        "status": "CODEX_TURN_STALLED",
                        "blockers": [
                            *snapshot.blockers,
                            "Codex turn has no semantic progress; runtime circuit is open",
                        ],
                        "next_steps": ["Explicit runtime recovery is required"],
                    }
                )
        return snapshot

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> Any:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        if handle.task_id and self.runtime_manager is not None:
            assert_runtime_circuit_closed(
                self._required_runtime(handle.task_id)
            )
        return await self.backend.steer(handle, instruction, lease=lease)

    async def interrupt(self, handle: PlanHandle) -> Any:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        if self.runtime_manager is None:
            return await self.backend.interrupt(handle)
        if not handle.task_id:
            raise CodexRuntimeAffinityError("interrupt handle has no task affinity")
        runtime = self._required_runtime(handle.task_id)
        if runtime.circuit_open and runtime.circuit_reason != "CODEX_TURN_STALLED":
            raise CodexRuntimeCircuitOpenError(
                "CODEX_RUNTIME_CIRCUIT_OPEN: protocol interrupt is not allowed"
            )
        mark_protocol_interrupt_attempted(
            self.memory.store,
            handle.task_id,
            runtime_instance_id=handle.runtime_instance_id,
            runtime_epoch=handle.runtime_epoch,
        )
        try:
            snapshot = self._stamp_snapshot(await self.backend.interrupt(handle), handle)
            status = (snapshot.status or "").strip().lower()
            if snapshot.reconciliation_required or status in {
                "unknown",
                "reconciliation_required",
                "compensation_required",
            }:
                open_runtime_circuit(
                    self.memory.store,
                    handle.task_id,
                    reason="TURN_INTERRUPT_TIMEOUT",
                    remote_status="APP_SERVER_UNRESPONSIVE",
                )
            logger.info(
                "turn interrupt completed task_id=%s instance_id=%s epoch=%s status=%s",
                handle.task_id,
                handle.runtime_instance_id,
                handle.runtime_epoch,
                snapshot.status,
            )
            return snapshot
        except Exception as exc:
            reason = (
                "TURN_INTERRUPT_TIMEOUT"
                if "timeout" in type(exc).__name__.lower()
                else "INTERRUPT_FAILED"
            )
            open_runtime_circuit(
                self.memory.store,
                handle.task_id,
                reason=reason,
                remote_status="APP_SERVER_UNRESPONSIVE",
            )
            raise

    async def list_pending_interactions(
        self,
        handle: PlanHandle,
    ) -> list[PendingInteraction]:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        return [
            item.model_copy(
                update={
                    "runtime_instance_id": handle.runtime_instance_id,
                    "runtime_epoch": handle.runtime_epoch,
                }
            )
            for item in await self.backend.list_pending_interactions(handle)
        ]

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> Any:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        if (
            interaction.runtime_instance_id != handle.runtime_instance_id
            or interaction.runtime_epoch != handle.runtime_epoch
        ):
            raise CodexRuntimeAffinityError(
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED: pending interaction is stale"
            )
        if handle.task_id and self.runtime_manager is not None:
            assert_runtime_circuit_closed(self._required_runtime(handle.task_id))
        return await self.backend.respond_interaction(handle, interaction, response)

    async def resume(self, handle: PlanHandle) -> Any:
        await self._ensure_started()
        self._assert_handle_runtime(handle)
        return self._stamp_snapshot(await self.backend.resume(handle), handle)

    def _required_runtime(self, task_id: str) -> CodexRuntimeState:
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise CodexRuntimeAffinityError("Task has no Codex runtime state")
        return runtime

    def _stamp_handle(self, handle: PlanHandle, *, task_id: str) -> PlanHandle:
        if self.runtime_manager is None:
            return handle.model_copy(update={"task_id": task_id})
        metadata = self.runtime_manager.refresh()
        if not metadata.isolation_verified:
            raise CodexRuntimeAffinityError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: runtime is not verified"
            )
        return handle.model_copy(
            update={
                "task_id": task_id,
                "runtime_instance_id": metadata.instance_id,
                "runtime_epoch": metadata.runtime_epoch,
                "runtime_ownership": metadata.ownership.value,
                "isolation_verified": metadata.isolation_verified,
            }
        )

    @staticmethod
    def _stamp_snapshot(snapshot: Any, handle: PlanHandle) -> Any:
        interactions = [
            item.model_copy(
                update={
                    "runtime_instance_id": handle.runtime_instance_id,
                    "runtime_epoch": handle.runtime_epoch,
                }
            )
            for item in list(getattr(snapshot, "pending_interactions", []) or [])
        ]
        return snapshot.model_copy(
            update={
                "runtime_instance_id": handle.runtime_instance_id,
                "runtime_epoch": handle.runtime_epoch,
                "runtime_ownership": handle.runtime_ownership,
                "isolation_verified": handle.isolation_verified,
                "pending_interactions": interactions,
            }
        )

    def _assert_handle_runtime(
        self,
        handle: PlanHandle | None,
        *,
        task_id: str | None = None,
        require_existing: bool = False,
    ) -> None:
        if self.runtime_manager is None:
            return
        target_task = task_id or (handle.task_id if handle else None)
        if not target_task:
            if self.runtime_manager is not None:
                raise CodexRuntimeAffinityError("runtime handle has no task affinity")
            return
        runtime = get_codex_runtime(self.memory.store, target_task)
        if runtime is None:
            if require_existing:
                raise CodexRuntimeAffinityError("Task has no Codex runtime state")
            return
        metadata = self.runtime_manager.refresh()
        assert_runtime_affinity(
            runtime,
            instance_id=(handle.runtime_instance_id if handle else metadata.instance_id),
            runtime_epoch=(handle.runtime_epoch if handle else metadata.runtime_epoch),
        )
        if handle is not None and (
            handle.runtime_instance_id != metadata.instance_id
            or handle.runtime_epoch != metadata.runtime_epoch
        ):
            raise CodexRuntimeAffinityError(
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED: handle belongs to a prior runtime"
            )

    def _task_ids_requiring_runtime_recovery(self) -> list[str]:
        return list_runtime_affinity_task_ids(self.memory.store)

    def _handle_for_runtime(self, runtime: CodexRuntimeState) -> PlanHandle:
        return PlanHandle(
            task_id=runtime.task_id,
            operation_id=runtime.operation_id,
            workflow_id=runtime.workflow_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            runtime_instance_id=runtime.runtime_instance_id,
            runtime_epoch=runtime.runtime_epoch,
            runtime_ownership=runtime.runtime_ownership,
            isolation_verified=runtime.isolation_verified,
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
                task = self.memory.get_task(task_id)
                runtime = get_codex_runtime(self.memory.store, task_id)
                execution = get_execution_state(self.memory.store, task_id)
                safety = get_agent_safety(self.memory.store, task_id)
            except ConflictError:
                continue
            plan_runtime = runtime is not None and (
                task.phase == TaskPhase.PLANNING
                or is_plan_runtime(runtime.remote_status)
            )
            recoverable_plan_latch = (
                plan_runtime
                and safety is not None
                and safety.state == AgentSafetyState.RECONCILIATION_REQUIRED.value
                and safety.operation == "runtime_recovery"
                and safety.details.get("recovery_reason")
                == "workspace-write runtime requires an active CODEX writer lease"
                and runtime is not None
                and all(
                    getattr(safety, field) == getattr(runtime, field)
                    for field in (
                        "workflow_id",
                        "operation_id",
                        "thread_id",
                        "turn_id",
                    )
                )
            )
            if (
                safety is not None
                and safety.state != AgentSafetyState.NONE.value
                and not recoverable_plan_latch
            ):
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
            if runtime is not None and runtime.circuit_open:
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="RUNTIME_RECOVERY_REQUIRED",
                        detail=(
                            runtime.circuit_reason
                            or "Codex runtime circuit is open"
                        ),
                    )
                )
                continue
            if not is_active_runtime(runtime.remote_status if runtime else None):
                continue
            if (
                not plan_runtime
                and is_execution_runtime(runtime.remote_status)
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
            if self.runtime_manager is not None:
                metadata = self.runtime_manager.refresh()
                try:
                    assert_runtime_affinity(
                        runtime,
                        instance_id=metadata.instance_id,
                        runtime_epoch=metadata.runtime_epoch,
                    )
                except CodexRuntimeAffinityError:
                    await self._latch_reconciliation(
                        task_id,
                        runtime,
                        "persisted thread belongs to a different runtime instance/epoch",
                    )
                    outcomes.append(
                        SessionRecoveryOutcome(
                            task_id=task_id,
                            status="RECONCILIATION_REQUIRED",
                            detail="runtime instance/epoch affinity cannot be proven",
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
            if plan_runtime and status == "not_reconstructable":
                current = self.memory.get_task(task_id)
                _, stopped_runtime = bind_codex_runtime(
                    self.memory.store,
                    task_id,
                    current.revision,
                    event_type=EventType.CODEX_INTERRUPTED,
                    workflow_id=runtime.workflow_id,
                    operation_id=runtime.operation_id,
                    thread_id=runtime.thread_id,
                    turn_id=runtime.turn_id,
                    remote_status="not_reconstructable",
                    task_phase=TaskPhase.PAUSED,
                    current_state=(
                        "Read-only Codex plan could not be resumed after restart; "
                        "start a new plan."
                    ),
                    event_payload={"restart_recovery": "plan_not_reconstructable"},
                )
                if recoverable_plan_latch:
                    record_agent_compensation_succeeded(
                        self.memory.store,
                        task_id,
                        operation="runtime_recovery",
                        summary=(
                            "The stale read-only planning runtime was confirmed absent; "
                            "the recovery latch was cleared safely."
                        ),
                        details={"recovery_result": "plan_not_reconstructable"},
                        workflow_id=stopped_runtime.workflow_id,
                        operation_id=stopped_runtime.operation_id,
                        thread_id=stopped_runtime.thread_id,
                        turn_id=stopped_runtime.turn_id,
                    )
                outcomes.append(
                    SessionRecoveryOutcome(
                        task_id=task_id,
                        status="PLAN_RESTART_REQUIRED",
                        detail=(
                            "the previous read-only plan runtime is no longer active; "
                            "a new plan may be started safely"
                        ),
                    )
                )
                continue
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
            if recoverable_plan_latch:
                record_agent_compensation_succeeded(
                    self.memory.store,
                    task_id,
                    operation="runtime_recovery",
                    summary=(
                        "The read-only planning runtime was confirmed after restart; "
                        "the recovery latch was cleared safely."
                    ),
                    details={"recovery_result": "plan_resumed"},
                    workflow_id=runtime.workflow_id,
                    operation_id=runtime.operation_id,
                    thread_id=runtime.thread_id,
                    turn_id=runtime.turn_id,
                )
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
            "runtime": self.runtime_manager.public_status() if self.runtime_manager else None,
            "runtime_error": self.runtime_error,
        }
