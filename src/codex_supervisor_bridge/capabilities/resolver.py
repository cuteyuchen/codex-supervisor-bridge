from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus


class CapabilityKind(str, Enum):
    LOCAL_WORKSPACE = "LOCAL_WORKSPACE"
    CODEX = "CODEX"
    DELIVERY = "DELIVERY"


class CapabilityStatus(BaseModel):
    capability: CapabilityKind
    label: str
    status: BackendHealthStatus
    message: str
    repairable: bool = False
    repair_action: str | None = None


class ResolvedBackends(BaseModel):
    workspace_backend: str | None = None
    agent_backend: str | None = None
    delivery_backend: str | None = None
    profile: str = "unavailable"
    capabilities: list[CapabilityStatus] = Field(default_factory=list)

    def user_summary(self) -> list[dict[str, str | bool | None]]:
        return [
            {
                "label": item.label,
                "status": item.status.value,
                "message": item.message,
                "repairable": item.repairable,
                "repair_action": item.repair_action,
            }
            for item in self.capabilities
        ]


class CapabilityResolver:
    """Choose internal backends while keeping implementation names out of normal UX."""

    def __init__(self, health: dict[str, BackendHealth]) -> None:
        self.health = health

    def _best(self, candidates: tuple[str, ...]) -> str | None:
        for desired in (BackendHealthStatus.READY, BackendHealthStatus.DEGRADED):
            for name in candidates:
                probe = self.health.get(name)
                if probe is not None and probe.status == desired:
                    return name
        return None

    def _public_capability(
        self,
        capability: CapabilityKind,
        label: str,
        selected: str | None,
        *,
        unavailable_message: str,
        repair_action: str,
    ) -> CapabilityStatus:
        if selected is None:
            return CapabilityStatus(
                capability=capability,
                label=label,
                status=BackendHealthStatus.UNAVAILABLE,
                message=unavailable_message,
                repairable=True,
                repair_action=repair_action,
            )
        probe = self.health[selected]
        if probe.status == BackendHealthStatus.READY:
            message = f"{label} is ready."
        else:
            message = f"{label} is available with a recoverable limitation."
        return CapabilityStatus(
            capability=capability,
            label=label,
            status=probe.status,
            message=message,
            repairable=probe.repairable,
            repair_action=repair_action if probe.repairable else None,
        )

    def resolve(self) -> ResolvedBackends:
        workspace = self._best(("devspace", "kandev"))
        lcb = self.health.get("local_codex_bridge")
        lcb_isolated = bool(
            lcb
            and lcb.status in {BackendHealthStatus.READY, BackendHealthStatus.DEGRADED}
            and lcb.capabilities.get("supports_isolated_runtime", False)
        )
        agent = (
            "local_codex_bridge"
            if lcb_isolated
            else self._best(("control_plane",))
        )
        codex = self._best(("codex",))
        delivery = self._best(("github", "kandev"))

        if workspace == "devspace" and agent == "local_codex_bridge":
            profile = "lightweight"
        elif workspace == "kandev" and agent == "control_plane":
            profile = "existing"
        elif workspace or agent or delivery:
            profile = "mixed"
        else:
            profile = "unavailable"

        return ResolvedBackends(
            workspace_backend=workspace,
            agent_backend=agent,
            delivery_backend=delivery,
            profile=profile,
            capabilities=[
                self._public_capability(
                    CapabilityKind.LOCAL_WORKSPACE,
                    "Local workspace",
                    workspace,
                    unavailable_message="Local workspace is not ready.",
                    repair_action="repair_local_workspace",
                ),
                self._public_capability(
                    CapabilityKind.CODEX,
                    "Codex",
                    codex,
                    unavailable_message="Codex control is not ready.",
                    repair_action="repair_codex",
                ),
                self._public_capability(
                    CapabilityKind.DELIVERY,
                    "GitHub delivery",
                    delivery,
                    unavailable_message="GitHub delivery is not ready.",
                    repair_action="reconnect_github",
                ),
            ],
        )
