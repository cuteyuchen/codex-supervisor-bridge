from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from mcp import Client

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    PendingInteraction,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.mcp.server import _resolve_startup_binding, create_mcp_server
from codex_supervisor_bridge.memory.agent_safety import (
    AgentSafetyState,
    get_agent_safety,
)
from codex_supervisor_bridge.memory.backend_binding import (
    TaskBackendBinding,
    bind_task_backend,
)
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
)
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import bind_workspace
from codex_supervisor_bridge.supervisor.agent_execution import AgentExecutionCoordinator
from codex_supervisor_bridge.supervisor.agent_session import AgentSessionManager
from codex_supervisor_bridge.supervisor.runtime import RuntimeComposition
from codex_supervisor_bridge.supervisor.runtime_resolver import RuntimeResolver


class StartupFakeAgent:
    def __init__(
        self,
        *,
        health_status: BackendHealthStatus = BackendHealthStatus.READY,
        resume_status: str = "running",
        resume_reconciliation: bool = False,
    ) -> None:
        self.health_status = health_status
        self.resume_status = resume_status
        self.resume_reconciliation = resume_reconciliation
        self.entered = 0
        self.exited = 0
        self.health_calls = 0
        self.resume_calls = 0

    async def __aenter__(self) -> "StartupFakeAgent":
        self.entered += 1
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.exited += 1

    async def health(self) -> BackendHealth:
        self.health_calls += 1
        return BackendHealth(
            capability="fake-agent",
            status=self.health_status,
            user_message="Codex control is ready.",
        )

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        self.resume_calls += 1
        return AgentSnapshot(
            status=self.resume_status,
            reconciliation_required=self.resume_reconciliation,
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        del task_id, context_pack, workspace
        raise AssertionError("not used by startup E2E")

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return AgentSnapshot(status="completed", **self._identity(handle))

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        del task_id, context_pack, approved_plan, workspace, lease
        raise AssertionError("not used by startup E2E")

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        del cursor, wait_ms
        return AgentSnapshot(status="running", **self._identity(handle))

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        del instruction, lease
        return await self.observe(handle)

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        return AgentSnapshot(status="interrupted", **self._identity(handle))

    async def list_pending_interactions(self, handle: PlanHandle) -> list[PendingInteraction]:
        del handle
        return []

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot:
        del interaction, response
        return await self.observe(handle)

    @staticmethod
    def _identity(handle: PlanHandle) -> dict[str, str | None]:
        return {
            "workflow_id": handle.workflow_id,
            "operation_id": handle.operation_id,
            "thread_id": handle.thread_id,
            "turn_id": handle.turn_id,
        }


class StartupFakeWorkspace:
    def __init__(
        self,
        *,
        status: BackendHealthStatus = BackendHealthStatus.READY,
    ) -> None:
        self.status = status

    async def __aenter__(self) -> "StartupFakeWorkspace":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake-workspace",
            status=self.status,
            user_message="Local workspace is ready.",
        )

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        del worktree, base_ref
        return WorkspaceState(workspace_id="ws", repository=repository, root="C:/repo")

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return f"{workspace_id}:{path}:{start_line}:{end_line}"

    async def apply_patch(
        self,
        workspace_id: str,
        patch: str,
        *,
        lease: WriterLeaseToken,
    ) -> Any:
        del workspace_id, patch, lease
        raise AssertionError("not used")

    async def run_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("not used")

    async def poll_command(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("not used")

    async def show_changes(self, workspace_id: str) -> Any:
        del workspace_id
        raise AssertionError("not used")

    async def git_state(self, workspace_id: str) -> Any:
        del workspace_id
        raise AssertionError("not used")

    async def close_workspace(self, workspace_id: str) -> None:
        del workspace_id
        return None


def _health(
    capability: str,
    status: BackendHealthStatus = BackendHealthStatus.READY,
) -> BackendHealth:
    return BackendHealth(
        capability=capability,
        status=status,
        user_message=f"{capability} is ready.",
    )


def _profile_b_health() -> dict[str, BackendHealth]:
    return {
        "devspace": _health("devspace"),
        "local_codex_bridge": _health("local_codex_bridge"),
        "codex": _health("codex"),
        "github": _health("github"),
    }


def _composition_b(
    memory: MemoryService,
    *,
    agent: StartupFakeAgent,
) -> tuple[RuntimeComposition, AgentSessionManager]:
    session = AgentSessionManager(
        memory,
        lambda: agent,
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    coordinator = AgentExecutionCoordinator(memory, session)
    return (
        RuntimeComposition(
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            workspace_factory=lambda: StartupFakeWorkspace(),
            agent_coordinator=coordinator,
            session_manager=session,
        ),
        session,
    )


def _run_profile_b_healthy_scenario() -> tuple[dict[str, Any], StartupFakeAgent]:
    memory = MemoryService()
    agent = StartupFakeAgent()
    try:
        selection = RuntimeResolver(_profile_b_health()).resolve()
        assert selection.profile == "lightweight"
        composition, session = _composition_b(memory, agent=agent)
        facade = composition.agent_facade(memory)
        server = create_mcp_server(
            memory,
            agent_facade=facade,
            checkpoints=composition.checkpoint_service,
            direct_workspace=None,
        )

        async def scenario() -> dict[str, Any]:
            outcomes = await composition.start()
            readiness = await composition.readiness()
            assert outcomes == []
            assert readiness.status == "READY"
            async with Client(server) as client:
                first = await client.call_tool("get_codex_control_health", {})
                assert first.is_error is False
                rendered = "\n".join(
                    getattr(item, "text", "")
                    for item in first.content
                    if getattr(item, "text", None)
                )
                assert "profile" in rendered
            await composition.shutdown()
            return {
                "readiness_status": readiness.status,
                "session_count": session.session_count,
                "shutdown_count": session.shutdown_count,
                "agent_entered": agent.entered,
                "agent_exited": agent.exited,
            }

        result = asyncio.run(scenario())
        assert result["session_count"] == 1
        assert result["shutdown_count"] == 1
        assert result["agent_entered"] == 1
        assert result["agent_exited"] == 1
        assert session.connected is False
        return result, agent
    finally:
        memory.close()


def test_startup_e2e_profile_b_healthy_first_health_ready_shutdown_once() -> None:
    result, agent = _run_profile_b_healthy_scenario()
    assert result["readiness_status"] == "READY"
    assert agent.health_calls >= 1


def test_startup_e2e_profile_a_fallback_when_profile_b_broken() -> None:
    memory = MemoryService()
    try:
        health = {
            "devspace": _health("devspace", BackendHealthStatus.UNAVAILABLE),
            "local_codex_bridge": _health(
                "local_codex_bridge",
                BackendHealthStatus.UNAVAILABLE,
            ),
            "codex": _health("codex", BackendHealthStatus.DEGRADED),
            "kandev": _health("kandev"),
            "control_plane": _health("control_plane"),
        }
        selection = RuntimeResolver(health).resolve()
        assert selection.profile == "existing"

        agent = StartupFakeAgent()
        session = AgentSessionManager(
            memory,
            lambda: agent,
            profile="existing",
            workspace_backend="kandev",
            agent_backend="control_plane",
        )
        coordinator = AgentExecutionCoordinator(memory, session)
        composition = RuntimeComposition(
            profile="existing",
            workspace_backend="kandev",
            agent_backend="control_plane",
            workspace_factory=lambda: StartupFakeWorkspace(),
            agent_coordinator=coordinator,
            session_manager=session,
        )

        async def scenario() -> None:
            await composition.start()
            readiness = await composition.readiness()
            assert readiness.profile == "existing"
            assert readiness.status == "READY"
            assert readiness.workspace_backend == "kandev"
            await composition.shutdown()

        asyncio.run(scenario())
        assert session.shutdown_count == 1
        assert agent.exited == 1
    finally:
        memory.close()


def test_startup_e2e_bound_task_never_falls_back_when_profile_b_temporarily_broken() -> None:
    binding = TaskBackendBinding(
        task_id="BOUND-STARTUP",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        profile="lightweight",
        bound_revision=1,
        bound_epoch=1,
    )
    health = {
        "devspace": _health("devspace", BackendHealthStatus.UNAVAILABLE),
        "local_codex_bridge": _health(
            "local_codex_bridge",
            BackendHealthStatus.UNAVAILABLE,
        ),
        "codex": _health("codex"),
        "kandev": _health("kandev"),
        "control_plane": _health("control_plane"),
    }
    selection = RuntimeResolver(health, task_binding=binding).resolve()
    assert selection.profile == "lightweight"
    assert selection.binding_forced is True
    assert selection.fallback_allowed is False
    assert selection.status == "UNAVAILABLE"


def test_startup_reconciliation_happens_before_ready(tmp_path: Path) -> None:
    database = tmp_path / "startup-recovery.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("STARTUP-REC", "Recover", repository="C:/repo")
        bind_workspace(
            memory.store,
            task.task_id,
            task.revision,
            backend_name="devspace",
            workspace_id="ws-recover",
            repository="C:/repo",
            root="C:/worktrees/ws-recover",
            workspace_mode="worktree",
        )
        task = memory.get_task(task.task_id)
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CODEX,
            explicit_user_authorization=True,
        )
        bind_codex_runtime(
            memory.store,
            task.task_id,
            acquired.task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-recover",
            operation_id="op-recover",
            thread_id="thread-recover",
            turn_id="turn-recover",
            remote_status="executing",
        )
    finally:
        memory.close()

    reopened = MemoryService(database)
    agent = StartupFakeAgent(
        resume_status="unknown",
        resume_reconciliation=True,
    )
    try:
        composition, session = _composition_b(reopened, agent=agent)

        async def scenario() -> None:
            outcomes = await composition.start()
            readiness = await composition.readiness()
            assert any(
                outcome.status == "RECONCILIATION_REQUIRED"
                for outcome in outcomes
            )
            assert readiness.startup_blockers
            assert readiness.status == "DEGRADED"
            safety = get_agent_safety(reopened.store, "STARTUP-REC")
            assert safety is not None
            assert safety.state == AgentSafetyState.RECONCILIATION_REQUIRED
            current = reopened.get_task("STARTUP-REC")
            execution = get_execution_state(reopened.store, "STARTUP-REC")
            with pytest.raises(ConflictError, match="compensation requires reconciliation"):
                handoff_writer(
                    reopened.store,
                    "STARTUP-REC",
                    current.revision,
                    from_writer=ActiveWriter.CODEX,
                    to_writer=ActiveWriter.CHATGPT,
                    expected_writer_epoch=execution.writer_epoch,
                    reason="Attempt handback while recovery is unresolved",
                )
            await composition.shutdown()

        asyncio.run(scenario())
        assert session.shutdown_count == 1
        assert agent.resume_calls == 1
    finally:
        reopened.close()


def test_runtime_readiness_codex_not_ready_degrades_profile() -> None:
    memory = MemoryService()
    try:
        agent = StartupFakeAgent()
        composition, session = _composition_b(memory, agent=agent)
        composition.codex_readiness = _health(
            "codex",
            BackendHealthStatus.DEGRADED,
        )

        async def scenario() -> None:
            await composition.start()
            readiness = await composition.readiness()
            assert readiness.workspace_status == "READY"
            assert readiness.agent_status == "READY"
            assert readiness.codex_status == "DEGRADED"
            assert readiness.status == "DEGRADED"
            assert readiness.requires_user_action is True
            await composition.shutdown()

        asyncio.run(scenario())
        assert session.shutdown_count == 1
    finally:
        memory.close()


def test_conflicting_active_task_bindings_fail_closed() -> None:
    memory = MemoryService()
    try:
        for index, binding in enumerate(
            (
                ("devspace", "local_codex_bridge", "lightweight"),
                ("kandev", "control_plane", "existing"),
            )
        ):
            task = memory.create_task(
                f"CONFLICT-{index}",
                "Conflict",
                repository="C:/repo",
            )
            acquired = acquire_writer(
                memory.store,
                task.task_id,
                task.revision,
                ActiveWriter.CODEX,
                explicit_user_authorization=True,
            )
            bind_task_backend(
                memory.store,
                task.task_id,
                acquired.task.revision,
                workspace_backend=binding[0],
                agent_backend=binding[1],
                profile=binding[2],
            )

        with pytest.raises(RuntimeError, match="STARTUP_RECONCILIATION_REQUIRED"):
            _resolve_startup_binding(memory)
    finally:
        memory.close()


def test_startup_uses_persisted_active_binding_not_healthier_profile() -> None:
    memory = MemoryService()
    try:
        task = memory.create_task("BOUND-ACTIVE", "Bound", repository="C:/repo")
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
        )
        bind_task_backend(
            memory.store,
            task.task_id,
            acquired.task.revision,
            workspace_backend="kandev",
            agent_backend="control_plane",
            profile="existing",
        )
        forced = _resolve_startup_binding(memory)
        assert forced is not None
        assert forced.profile == "existing"
        health = {
            "devspace": _health("devspace"),
            "local_codex_bridge": _health("local_codex_bridge"),
            "codex": _health("codex"),
            "kandev": _health("kandev", BackendHealthStatus.UNAVAILABLE),
            "control_plane": _health(
                "control_plane",
                BackendHealthStatus.UNAVAILABLE,
            ),
        }
        selection = RuntimeResolver(health, task_binding=forced).resolve()
        assert selection.profile == "existing"
        assert selection.binding_forced is True
        assert selection.status == "UNAVAILABLE"
    finally:
        memory.close()
