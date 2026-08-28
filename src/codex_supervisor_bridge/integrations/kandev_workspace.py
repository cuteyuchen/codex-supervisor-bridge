from __future__ import annotations

from collections.abc import Callable
from typing import Any

from codex_supervisor_bridge.backends.models import (
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    CommandResult,
    GitState,
    WorkspaceState,
    WriterLeaseToken,
)

from .kandev_client import KandevAdapter
from .kandev_errors import (
    KandevCapabilityError,
    KandevError,
    KandevWorkspaceUnavailableError,
    KandevWorkspaceUnsupportedError,
)

KandevAdapterFactory = Callable[[], KandevAdapter]

_REPOSITORY_KEYS = (
    "local_path",
    "path",
    "repository",
    "repository_url",
    "name",
    "id",
    "workspace_id",
    "workspaceId",
)


def _iter_payload(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key in ("workspaces", "items", "results", "data"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def _repository_values(item: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in _REPOSITORY_KEYS:
        raw = item.get(key)
        if isinstance(raw, str) and raw.strip():
            values.append(raw.strip())
    return values


def _matches(repository: str, item: dict[str, Any]) -> bool:
    normalized = repository.replace("\\", "/").rstrip("/").lower()
    for value in _repository_values(item):
        candidate = value.replace("\\", "/").rstrip("/").lower()
        if normalized and candidate and (
            candidate == normalized
            or candidate.endswith("/" + normalized.rsplit("/", 1)[-1])
        ):
            return True
    return False


class KandevWorkspaceBackend:
    """WorkspaceBackend adapter for the Profile A Kandev fallback.

    Kandev owns the external worktree. The adapter can select a managed
    workspace and report protocol health, but it does not invent direct
    read/patch/command capabilities that the upstream Kandev MCP does not
    expose; unsupported direct mutations fail closed with a provider error.
    """

    def __init__(self, adapter_factory: KandevAdapterFactory) -> None:
        self.adapter_factory = adapter_factory

    async def __aenter__(self) -> "KandevWorkspaceBackend":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        try:
            async with self.adapter_factory() as adapter:
                capabilities = await adapter.require_compatible()
        except KandevCapabilityError as exc:
            return BackendHealth(
                capability="kandev",
                status=BackendHealthStatus.DEGRADED,
                user_message="Fallback workspace needs an update or repair.",
                repairable=True,
                technical_detail="missing tools: " + ", ".join(exc.missing_tools),
            )
        except KandevError as exc:
            return BackendHealth(
                capability="kandev",
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Fallback workspace is not ready.",
                repairable=True,
                technical_detail=str(exc),
            )
        except Exception as exc:  # noqa: BLE001 - health probes fail closed
            return BackendHealth(
                capability="kandev",
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Fallback workspace is not ready.",
                repairable=True,
                technical_detail=f"{type(exc).__name__}: {exc}",
            )
        return BackendHealth(
            capability="kandev",
            status=BackendHealthStatus.READY,
            user_message="Fallback workspace is ready.",
            technical_detail=f"tool_count={len(capabilities.tools)}",
        )

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        del worktree
        async with self.adapter_factory() as adapter:
            payload = await adapter.list_workspaces()
        for item in _iter_payload(payload):
            if _matches(repository, item):
                workspace_id = (
                    item.get("workspace_id")
                    or item.get("workspaceId")
                    or item.get("id")
                )
                root = item.get("local_path") or item.get("path")
                if not isinstance(workspace_id, str) or not workspace_id:
                    continue
                return WorkspaceState(
                    workspace_id=workspace_id,
                    repository=repository,
                    root=root if isinstance(root, str) and root else None,
                    worktree=False,
                    git=GitState(
                        branch=base_ref,
                    ),
                )
        raise KandevWorkspaceUnavailableError(
            f"No Kandev workspace matches repository {repository!r}"
        )

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        del workspace_id, path, start_line, end_line
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct workspace reads"
        )

    async def apply_patch(
        self,
        workspace_id: str,
        patch: str,
        *,
        lease: WriterLeaseToken,
    ) -> ChangeReview:
        del workspace_id, patch, lease
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct workspace patches"
        )

    async def run_command(
        self,
        workspace_id: str,
        command: str,
        *,
        lease: WriterLeaseToken,
        yield_time_ms: int = 10_000,
        tty: bool = False,
    ) -> CommandResult:
        del workspace_id, command, lease, yield_time_ms, tty
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct command execution"
        )

    async def poll_command(
        self,
        workspace_id: str,
        command_id: str,
        *,
        input_text: str | None = None,
        interrupt: bool = False,
    ) -> CommandResult:
        del workspace_id, command_id, input_text, interrupt
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct command sessions"
        )

    async def show_changes(self, workspace_id: str) -> ChangeReview:
        del workspace_id
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct change reviews"
        )

    async def git_state(self, workspace_id: str) -> GitState:
        del workspace_id
        raise KandevWorkspaceUnsupportedError(
            "Kandev Profile A does not expose direct Git state reads"
        )

    async def close_workspace(self, workspace_id: str) -> None:
        del workspace_id
        return None
