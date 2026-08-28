from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codex_supervisor_bridge.backends.workspace import WorkspaceBackend
from codex_supervisor_bridge.memory.service import MemoryService

from .agent_execution import AgentExecutionCoordinator
from .agent_facade import AgentSupervisorFacade
from .agent_session import AgentSessionManager
from .checkpoints import CheckpointService

WorkspaceAdapterFactory = Callable[[], AbstractAsyncContextManager[WorkspaceBackend]]


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
    delivery_backend: str = "github"

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
            workspace_factory=workspace_factory or cls._default_devspace_workspace_factory(),
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


def lcb_environment() -> dict[str, str]:
    """Base environment for the LCB stdio process without leaking secrets."""
    return dict(os.environ)
