from __future__ import annotations

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.capabilities import CapabilityResolver


def health(
    capability: str,
    status: BackendHealthStatus,
    *,
    repairable: bool = False,
) -> BackendHealth:
    return BackendHealth(
        capability=capability,
        status=status,
        user_message=f"{capability} probe",
        repairable=repairable,
        technical_detail=f"technical:{capability}",
    )


def test_prefers_lightweight_profile_when_ready() -> None:
    resolved = CapabilityResolver(
        {
            "devspace": health("devspace", BackendHealthStatus.READY),
            "local_codex_bridge": health("local-codex", BackendHealthStatus.READY),
            "kandev": health("kandev", BackendHealthStatus.READY),
            "control_plane": health("control-plane", BackendHealthStatus.READY),
            "github": health("github", BackendHealthStatus.READY),
        }
    ).resolve()
    assert resolved.profile == "lightweight"
    assert resolved.workspace_backend == "devspace"
    assert resolved.agent_backend == "local_codex_bridge"
    assert resolved.delivery_backend == "github"


def test_falls_back_to_existing_profile_without_user_backend_choice() -> None:
    resolved = CapabilityResolver(
        {
            "devspace": health("devspace", BackendHealthStatus.UNAVAILABLE),
            "local_codex_bridge": health("local-codex", BackendHealthStatus.UNAVAILABLE),
            "kandev": health("kandev", BackendHealthStatus.READY),
            "control_plane": health("control-plane", BackendHealthStatus.READY),
            "github": health("github", BackendHealthStatus.READY),
        }
    ).resolve()
    assert resolved.profile == "existing"
    assert resolved.workspace_backend == "kandev"
    assert resolved.agent_backend == "control_plane"


def test_normal_health_summary_hides_backend_names_and_technical_details() -> None:
    resolved = CapabilityResolver(
        {
            "devspace": health("devspace", BackendHealthStatus.READY),
            "local_codex_bridge": health("local-codex", BackendHealthStatus.DEGRADED, repairable=True),
            "github": health("github", BackendHealthStatus.READY),
        }
    ).resolve()
    summary = resolved.user_summary()
    rendered = str(summary).lower()
    assert "devspace" not in rendered
    assert "local_codex_bridge" not in rendered
    assert "local-codex" not in rendered
    assert "technical:" not in rendered
    assert {item["label"] for item in summary} == {
        "Local workspace",
        "Codex",
        "GitHub delivery",
    }


def test_unavailable_capabilities_offer_compact_repair_actions() -> None:
    resolved = CapabilityResolver({}).resolve()
    summary = resolved.user_summary()
    assert resolved.profile == "unavailable"
    assert all(item["status"] == "UNAVAILABLE" for item in summary)
    assert {item["repair_action"] for item in summary} == {
        "repair_local_workspace",
        "repair_codex",
        "reconnect_github",
    }
