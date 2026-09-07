from __future__ import annotations

import asyncio
from typing import Any

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
from codex_supervisor_bridge.integrations.devspace_client import DevSpaceWorkspaceAdapter
from codex_supervisor_bridge.integrations.kandev_workspace import KandevWorkspaceBackend
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_execution import AgentExecutionCoordinator
from codex_supervisor_bridge.supervisor.agent_session import AgentSessionManager
from codex_supervisor_bridge.supervisor.runtime import RuntimeComposition


class FakeManagedBackend:
    def __init__(
        self,
        *,
        health_status: BackendHealthStatus = BackendHealthStatus.READY,
    ) -> None:
        self.health_status = health_status
        self.entered = 0
        self.exited = 0
        self.health_calls = 0
        self.resume_calls = 0

    async def __aenter__(self) -> "FakeManagedBackend":
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

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        del task_id, context_pack, workspace
        return PlanHandle(
            workflow_id="wf",
            thread_id="thread",
            turn_id="turn",
            status="planning",
        )

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
        return PlanHandle(
            operation_id="op",
            workflow_id="wf",
            thread_id="thread",
            turn_id="exec-turn",
            status="executing",
        )

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

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        self.resume_calls += 1
        return AgentSnapshot(status="running", **self._identity(handle))

    @staticmethod
    def _identity(handle: PlanHandle) -> dict[str, str | None]:
        return {
            "workflow_id": handle.workflow_id,
            "operation_id": handle.operation_id,
            "thread_id": handle.thread_id,
            "turn_id": handle.turn_id,
        }


class FakeWorkspace:
    def __init__(
        self,
        *,
        status: BackendHealthStatus = BackendHealthStatus.READY,
    ) -> None:
        self.status = status

    async def __aenter__(self) -> "FakeWorkspace":
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
        return WorkspaceState(
            workspace_id="ws",
            repository=repository,
            root="C:/repo",
            worktree=worktree,
        )

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        return f"{workspace_id}:{path}"

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


def _composition(
    memory: MemoryService,
    *,
    workspace_status: BackendHealthStatus = BackendHealthStatus.READY,
    agent_status: BackendHealthStatus = BackendHealthStatus.READY,
) -> tuple[RuntimeComposition, list[FakeManagedBackend]]:
    created: list[FakeManagedBackend] = []

    def backend_factory() -> FakeManagedBackend:
        backend = FakeManagedBackend(health_status=agent_status)
        created.append(backend)
        return backend

    session = AgentSessionManager(
        memory,
        backend_factory,
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    coordinator = AgentExecutionCoordinator(memory, session)
    composition = RuntimeComposition(
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        workspace_factory=lambda: FakeWorkspace(status=workspace_status),
        agent_coordinator=coordinator,
        session_manager=session,
    )
    return composition, created


def test_start_is_idempotent_and_shutdown_closes_exactly_once() -> None:
    memory = MemoryService()
    try:
        composition, created = _composition(memory)

        async def scenario() -> None:
            first = await composition.start()
            second = await composition.start()
            assert first == second == []
            await composition.shutdown()
            await composition.shutdown()

        asyncio.run(scenario())
        assert len(created) == 1
        assert created[0].entered == 1
        assert created[0].exited == 1
        assert composition.session_manager is not None
        assert composition.session_manager.session_count == 1
        assert composition.session_manager.shutdown_count == 1
    finally:
        memory.close()


def test_readiness_combines_workspace_agent_and_startup_blockers() -> None:
    memory = MemoryService()
    try:
        composition, _ = _composition(memory)

        async def scenario() -> None:
            await composition.start()
            readiness = await composition.readiness()
            assert readiness.status == "READY"
            assert readiness.workspace_status == "READY"
            assert readiness.agent_status == "READY"
            assert readiness.codex_status == "READY"
            assert readiness.requires_user_action is False

        asyncio.run(scenario())
    finally:
        memory.close()


def test_readiness_reports_unavailable_workspace_without_killing_supervisor() -> None:
    memory = MemoryService()
    try:
        composition, _ = _composition(
            memory,
            workspace_status=BackendHealthStatus.UNAVAILABLE,
        )

        async def scenario() -> None:
            await composition.start()
            readiness = await composition.readiness()
            assert readiness.status == "UNAVAILABLE"
            assert readiness.workspace_status == "UNAVAILABLE"
            assert readiness.requires_user_action is True

        asyncio.run(scenario())
    finally:
        memory.close()


def test_first_mcp_call_can_be_health_without_prior_start_plan() -> None:
    memory = MemoryService()
    try:
        composition, _ = _composition(memory)

        async def scenario() -> None:
            await composition.start()
            facade = composition.agent_facade(memory)
            server = create_mcp_server(
                memory,
                agent_facade=facade,
                checkpoints=composition.checkpoint_service,
                direct_workspace=None,
            )
            async with Client(server) as client:
                result = await client.call_tool("get_codex_control_health", {})
                assert result.is_error is False
                rendered = "\n".join(
                    getattr(item, "text", "")
                    for item in result.content
                    if getattr(item, "text", None)
                )
                assert "profile" in rendered
                assert "workspace_backend" in rendered

        asyncio.run(scenario())
    finally:
        memory.close()


def test_profile_a_factory_returns_kandev_and_profile_b_returns_devspace() -> None:
    memory = MemoryService()
    try:
        profile_b = RuntimeComposition.profile_b(
            memory,
            launch_command=["node", "dist/src/index.js"],
        )
        assert isinstance(profile_b.workspace_factory(), DevSpaceWorkspaceAdapter)
        assert profile_b.workspace_backend == "devspace"

        profile_a = RuntimeComposition.profile_a(
            memory,
            adapter_factory=lambda: object(),
        )
        assert isinstance(profile_a.workspace_factory(), KandevWorkspaceBackend)
        assert profile_a.workspace_backend == "kandev"
    finally:
        memory.close()
