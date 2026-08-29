from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from codex_supervisor_bridge.backends.models import (
    BackendHealth,
    BackendHealthStatus,
)
from codex_supervisor_bridge.backends.workspace import WorkspaceBackend
from codex_supervisor_bridge.memory.service import MemoryService

from .agent_execution import AgentExecutionCoordinator
from .agent_facade import AgentSupervisorFacade
from .agent_session import AgentSessionManager, SessionRecoveryOutcome
from .checkpoints import CheckpointService

WorkspaceAdapterFactory = Callable[[], AbstractAsyncContextManager[WorkspaceBackend]]
READINESS_PROBE_TIMEOUT_SECONDS = 15.0


class ProfileReadiness(BaseModel):
    """Combined workspace + agent + Codex + startup-recovery readiness."""

    profile: str
    status: str
    workspace_backend: str
    agent_backend: str
    workspace_status: str
    agent_status: str
    codex_status: str
    recovery_outcomes: list[SessionRecoveryOutcome] = Field(default_factory=list)
    startup_blockers: list[str] = Field(default_factory=list)
    requires_user_action: bool = False
    reason: str = ""


@dataclass
class RuntimeComposition:
    """Provider-neutral production runtime wiring for one backend profile.

    The Supervisor Core depends only on WorkspaceBackend/AgentBackend
    protocols. Provider adapters live behind the factories assembled here.
    """

    profile: str
    workspace_backend: str
    agent_backend: str
    workspace_factory: WorkspaceAdapterFactory
    agent_coordinator: AgentExecutionCoordinator
    session_manager: AgentSessionManager | None = None
    checkpoint_service: CheckpointService | None = None
    codex_readiness: BackendHealth | None = None
    delivery_backend: str = "github"
    started: bool = False
    recovery_outcomes: list[SessionRecoveryOutcome] = field(default_factory=list)

    @classmethod
    def profile_b(
        cls,
        memory: MemoryService,
        *,
        launch_command: list[str],
        env: dict[str, str] | None = None,
        workspace_factory: WorkspaceAdapterFactory | None = None,
        delivery_backend: str = "github",
    ) -> "RuntimeComposition":
        from codex_supervisor_bridge.integrations.agent_backends import (
            LocalCodexBridgeAgentBackend,
        )

        if not launch_command or not launch_command[0].strip():
            raise ValueError("Local-Codex-Bridge launch command must not be empty")

        def backend_factory() -> LocalCodexBridgeAgentBackend:
            return LocalCodexBridgeAgentBackend.stdio(
                launch_command[0],
                args=list(launch_command[1:]),
                env=env,
            )

        session = AgentSessionManager(
            memory,
            backend_factory,
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
        )
        coordinator = AgentExecutionCoordinator(memory, session)
        checkpoints = CheckpointService(memory, agent_backend=session)
        return cls(
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            workspace_factory=workspace_factory
            or cls._default_devspace_workspace_factory(),
            agent_coordinator=coordinator,
            session_manager=session,
            checkpoint_service=checkpoints,
            delivery_backend=delivery_backend,
        )

    @classmethod
    def profile_a(
        cls,
        memory: MemoryService,
        *,
        adapter_factory: Callable[[], Any],
        workspace_factory: WorkspaceAdapterFactory | None = None,
        kandev_adapter_factory: Callable[[], Any] | None = None,
        delivery_backend: str = "github",
    ) -> "RuntimeComposition":
        from codex_supervisor_bridge.integrations.agent_backends import (
            ControlPlaneAgentBackend,
        )

        session = AgentSessionManager(
            memory,
            lambda: ControlPlaneAgentBackend(adapter_factory),
            profile="existing",
            workspace_backend="kandev",
            agent_backend="control_plane",
        )
        coordinator = AgentExecutionCoordinator(memory, session)
        checkpoints = CheckpointService(memory, agent_backend=session)
        return cls(
            profile="existing",
            workspace_backend="kandev",
            agent_backend="control_plane",
            workspace_factory=workspace_factory
            or cls._default_kandev_workspace_factory(kandev_adapter_factory),
            agent_coordinator=coordinator,
            session_manager=session,
            checkpoint_service=checkpoints,
            delivery_backend=delivery_backend,
        )

    @staticmethod
    def _default_devspace_workspace_factory() -> WorkspaceAdapterFactory:
        from codex_supervisor_bridge.integrations.devspace_client import (
            DevSpaceWorkspaceAdapter,
        )

        return lambda: DevSpaceWorkspaceAdapter()

    @staticmethod
    def _default_kandev_workspace_factory(
        kandev_adapter_factory: Callable[[], Any] | None,
    ) -> WorkspaceAdapterFactory:
        from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
        from codex_supervisor_bridge.integrations.kandev_workspace import (
            KandevWorkspaceBackend,
        )

        adapter_factory = kandev_adapter_factory or (lambda: KandevAdapter())
        return lambda: KandevWorkspaceBackend(adapter_factory)

    async def start(self) -> list[SessionRecoveryOutcome]:
        """Start persistent owned resources and run startup recovery before READY."""
        if self.started:
            return list(self.recovery_outcomes)
        if self.session_manager is not None:
            self.recovery_outcomes = await self.session_manager.start()
        self.started = True
        return list(self.recovery_outcomes)

    async def shutdown(self) -> None:
        """Stop owned resources; safe to call more than once."""
        if self.session_manager is not None:
            await self.session_manager.shutdown()
        self.started = False

    async def readiness(self) -> ProfileReadiness:
        """Probe workspace and agent capabilities after startup recovery."""
        recovery = list(self.recovery_outcomes)
        if self.session_manager is not None:
            agent_health = await self.session_manager.health()
        else:
            agent_health = BackendHealth(
                capability=self.agent_backend,
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex control is not connected.",
                repairable=True,
                technical_detail="no AgentSessionManager is composed",
            )
        codex_health = self.codex_readiness or agent_health

        try:
            workspace_health = await asyncio.wait_for(
                self._probe_workspace(),
                timeout=READINESS_PROBE_TIMEOUT_SECONDS,
            )
        except Exception:
            workspace_health = BackendHealth(
                capability=self.workspace_backend,
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Local workspace is not ready.",
                repairable=True,
                technical_detail="workspace readiness probe failed",
            )

        blockers = [
            f"{outcome.task_id}: {outcome.status} - {outcome.detail}"
            for outcome in recovery
            if outcome.status == "RECONCILIATION_REQUIRED"
        ]
        status = self._combined_status(workspace_health.status, agent_health.status)
        status = self._combined_status(status, codex_health.status)
        if blockers:
            status = BackendHealthStatus.DEGRADED
        return ProfileReadiness(
            profile=self.profile,
            status=status.value,
            workspace_backend=self.workspace_backend,
            agent_backend=self.agent_backend,
            workspace_status=workspace_health.status.value,
            agent_status=agent_health.status.value,
            codex_status=codex_health.status.value,
            recovery_outcomes=recovery,
            startup_blockers=blockers,
            requires_user_action=status != BackendHealthStatus.READY or bool(blockers),
            reason=(
                "startup reconciliation blocks PROFILE_READY"
                if blockers
                else "combined workspace, agent and Codex readiness"
            ),
        )

    async def _probe_workspace(self) -> BackendHealth:
        async with self.workspace_factory() as adapter:
            return await adapter.health()

    @staticmethod
    def _combined_status(*statuses: BackendHealthStatus) -> BackendHealthStatus:
        rank = {
            BackendHealthStatus.READY: 0,
            BackendHealthStatus.DEGRADED: 1,
            BackendHealthStatus.UNAVAILABLE: 2,
        }
        return max(statuses, key=lambda status: rank[status])

    def agent_facade(self, memory: MemoryService) -> AgentSupervisorFacade:
        return AgentSupervisorFacade(
            memory,
            self.agent_coordinator,
            profile=self.profile,
            workspace_backend=self.workspace_backend,
            agent_backend=self.agent_backend,
            session_manager=self.session_manager,
        )


def lcb_launch_from_config(
    *,
    repository: str | Path,
    node_executable: str = "node",
) -> list[str]:
    """Resolve the canonical client-owned stdio launch command."""
    from codex_supervisor_bridge.bootstrap.local_codex import (
        LocalCodexBridgeBootstrap,
    )

    return LocalCodexBridgeBootstrap.canonical_launch_command(
        repository,
        node_executable=node_executable,
    )


def lcb_environment(
    *,
    app_data_root: str | Path | None = None,
) -> dict[str, str]:
    """Inherit the user environment for the LCB -> Codex stdio process.

    Provider credentials that already work for the user's Codex CLI are passed
    through to the child process exactly as the user has them configured.
    Values are never serialized into diagnostics, Context Packs, logs, or MCP
    responses.
    """

    environment = dict(os.environ)
    if app_data_root is None:
        from codex_supervisor_bridge.bootstrap.paths import AppDataPaths

        app_data_root = AppDataPaths.from_environment().root
    environment["CODEX_SUPERVISOR_DATA_DIR"] = str(Path(app_data_root))
    return environment
