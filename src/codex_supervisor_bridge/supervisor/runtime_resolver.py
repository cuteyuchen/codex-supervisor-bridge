from __future__ import annotations

from pydantic import BaseModel, Field

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.memory.backend_binding import TaskBackendBinding

_STATUS_RANK = {
    BackendHealthStatus.READY: 0,
    BackendHealthStatus.DEGRADED: 1,
    BackendHealthStatus.UNAVAILABLE: 2,
}


class RuntimeSelection(BaseModel):
    """Capability-driven production profile selection with binding enforcement."""

    profile: str
    workspace_backend: str | None = None
    agent_backend: str | None = None
    delivery_backend: str | None = None
    status: str = "UNAVAILABLE"
    reason: str
    requires_user_action: bool = False
    fallback_allowed: bool = True
    binding_forced: bool = False
    missing: list[str] = Field(default_factory=list)


class RuntimeResolver:
    """Resolve the production profile from real capability health.

    A task that already carries a durable backend binding is never silently
    switched when its profile is temporarily DEGRADED. Only an unbound task
    may use capability fallback between Profile B and Profile A.
    """

    PROFILES: dict[str, tuple[str, str]] = {
        "lightweight": ("devspace", "local_codex_bridge"),
        "existing": ("kandev", "control_plane"),
    }

    def __init__(
        self,
        health: dict[str, BackendHealth],
        *,
        development_style: str | None = None,
        task_binding: TaskBackendBinding | None = None,
    ) -> None:
        self.health = dict(health)
        self.development_style = development_style
        self.task_binding = task_binding

    def _probe(
        self,
        workspace_backend: str,
        agent_backend: str,
    ) -> tuple[BackendHealthStatus, list[str]]:
        statuses: list[BackendHealthStatus] = []
        missing: list[str] = []
        for name in (workspace_backend, agent_backend, "codex"):
            probe = self.health.get(name)
            if probe is None:
                statuses.append(BackendHealthStatus.UNAVAILABLE)
                missing.append(name)
            else:
                statuses.append(probe.status)
        combined = max(statuses, key=lambda status: _STATUS_RANK[status])
        return combined, missing

    def _delivery(self) -> str | None:
        probe = self.health.get("github")
        if probe is None:
            return None
        if probe.status in {BackendHealthStatus.READY, BackendHealthStatus.DEGRADED}:
            return "github"
        return None

    def _selection(
        self,
        profile: str,
        workspace_backend: str,
        agent_backend: str,
        status: BackendHealthStatus,
        *,
        reason: str,
        requires_user_action: bool,
        fallback_allowed: bool,
        binding_forced: bool = False,
        missing: list[str] | None = None,
    ) -> RuntimeSelection:
        return RuntimeSelection(
            profile=profile,
            workspace_backend=workspace_backend,
            agent_backend=agent_backend,
            delivery_backend=self._delivery(),
            status=status.value,
            reason=reason,
            requires_user_action=requires_user_action,
            fallback_allowed=fallback_allowed,
            binding_forced=binding_forced,
            missing=list(missing or []),
        )

    def resolve(self) -> RuntimeSelection:
        if self.task_binding is not None:
            binding = self.task_binding
            workspace_backend = binding.workspace_backend
            agent_backend = binding.agent_backend
            status, missing = self._probe(workspace_backend, agent_backend)
            return self._selection(
                binding.profile,
                workspace_backend,
                agent_backend,
                status,
                reason=(
                    "Task backend binding is enforced; a temporary capability "
                    "drop cannot silently switch an already-bound task"
                ),
                requires_user_action=status != BackendHealthStatus.READY or bool(missing),
                fallback_allowed=False,
                binding_forced=True,
                missing=missing,
            )

        b_status, b_missing = self._probe(*self.PROFILES["lightweight"])
        a_status, a_missing = self._probe(*self.PROFILES["existing"])
        if b_status == BackendHealthStatus.READY:
            return self._selection(
                "lightweight",
                *self.PROFILES["lightweight"],
                b_status,
                reason="Profile B capabilities are fully ready",
                requires_user_action=False,
                fallback_allowed=True,
            )
        if a_status == BackendHealthStatus.READY:
            return self._selection(
                "existing",
                *self.PROFILES["existing"],
                a_status,
                reason="Profile B is not ready and Profile A capabilities are ready",
                requires_user_action=False,
                fallback_allowed=True,
            )

        prefer_b = _STATUS_RANK[b_status] <= _STATUS_RANK[a_status]
        if self.development_style == "web_first":
            prefer_b = False
        profile = "lightweight" if prefer_b else "existing"
        status = b_status if prefer_b else a_status
        missing = b_missing if prefer_b else a_missing
        workspace_backend, agent_backend = self.PROFILES[profile]
        return self._selection(
            profile,
            workspace_backend,
            agent_backend,
            status,
            reason=(
                "No complete profile is ready; the nearest repairable candidate "
                "is selected for readiness reporting and repair"
            ),
            requires_user_action=True,
            fallback_allowed=True,
            missing=missing,
        )
