from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.backends.models import BackendHealthStatus
from codex_supervisor_bridge.integrations.kandev_errors import (
    KandevCapabilityError,
    KandevWorkspaceUnavailableError,
    KandevWorkspaceUnsupportedError,
)
from codex_supervisor_bridge.integrations.kandev_models import KandevCapabilities
from codex_supervisor_bridge.integrations.kandev_workspace import KandevWorkspaceBackend
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.runtime import RuntimeComposition


class FakeKandevAdapter:
    def __init__(
        self,
        *,
        compatible: bool = True,
        workspaces: list[dict[str, Any]] | None = None,
        unavailable: bool = False,
    ) -> None:
        self.compatible = compatible
        self.workspaces = workspaces or []
        self.unavailable = unavailable

    async def __aenter__(self) -> "FakeKandevAdapter":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def require_compatible(self) -> KandevCapabilities:
        if self.unavailable:
            raise RuntimeError("Kandev MCP is unreachable")
        if not self.compatible:
            raise KandevCapabilityError(["list_workflows_kandev"])
        return KandevCapabilities(tools=["list_workspaces_kandev", "list_workflows_kandev"])

    async def list_workspaces(self) -> dict[str, Any]:
        return {"workspaces": self.workspaces}


def _backend(**kwargs: Any) -> KandevWorkspaceBackend:
    return KandevWorkspaceBackend(lambda: FakeKandevAdapter(**kwargs))


async def _health_status(backend: KandevWorkspaceBackend) -> BackendHealthStatus:
    return (await backend.health()).status


def test_health_ready_when_required_tools_are_compatible() -> None:
    status = asyncio.run(_health_status(_backend()))
    assert status == BackendHealthStatus.READY


def test_health_degraded_when_required_tools_missing() -> None:
    status = asyncio.run(_health_status(_backend(compatible=False)))
    assert status == BackendHealthStatus.DEGRADED


def test_health_unavailable_when_provider_unreachable() -> None:
    status = asyncio.run(_health_status(_backend(unavailable=True)))
    assert status == BackendHealthStatus.UNAVAILABLE


def test_open_workspace_selects_matching_kandev_repository() -> None:
    backend = _backend(
        workspaces=[
            {
                "workspace_id": "kandev-1",
                "local_path": "C:/other",
                "name": "other",
            },
            {
                "workspace_id": "kandev-2",
                "local_path": "C:/repo",
                "name": "repo",
            },
        ]
    )

    opened = asyncio.run(backend.open_workspace("C:/repo"))

    assert opened.workspace_id == "kandev-2"
    assert opened.repository == "C:/repo"
    assert opened.root == "C:/repo"
    assert opened.worktree is False


def test_open_workspace_fails_closed_without_match() -> None:
    backend = _backend(
        workspaces=[
            {
                "workspace_id": "kandev-1",
                "local_path": "C:/other",
            }
        ]
    )

    with pytest.raises(KandevWorkspaceUnavailableError):
        asyncio.run(backend.open_workspace("C:/repo"))


def test_direct_workspace_operations_fail_closed_for_kandev() -> None:
    backend = _backend()

    async def scenario() -> None:
        for operation in (
            backend.read("ws", "src/app.py"),
            backend.apply_patch("ws", "patch", lease=object()),
            backend.run_command("ws", "pytest", lease=object()),
            backend.poll_command("ws", "cmd"),
            backend.show_changes("ws"),
            backend.git_state("ws"),
        ):
            with pytest.raises(KandevWorkspaceUnsupportedError):
                await operation

    asyncio.run(scenario())


def test_profile_a_workspace_factory_builds_kandev_backend() -> None:
    memory = MemoryService()
    try:
        composition = RuntimeComposition.profile_a(
            memory,
            adapter_factory=lambda: object(),
            kandev_adapter_factory=lambda: FakeKandevAdapter(),
        )
        adapter = composition.workspace_factory()
        assert isinstance(adapter, KandevWorkspaceBackend)
    finally:
        memory.close()
