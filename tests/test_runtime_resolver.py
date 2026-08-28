from __future__ import annotations

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.memory.backend_binding import TaskBackendBinding
from codex_supervisor_bridge.supervisor.runtime_resolver import RuntimeResolver


def _health(
    capability: str,
    status: BackendHealthStatus = BackendHealthStatus.READY,
) -> BackendHealth:
    return BackendHealth(
        capability=capability,
        status=status,
        user_message=f"{capability} is ready.",
        repairable=status != BackendHealthStatus.READY,
    )


def _profile_b_ready() -> dict[str, BackendHealth]:
    return {
        "devspace": _health("devspace"),
        "local_codex_bridge": _health("local_codex_bridge"),
        "github": _health("github"),
    }


def test_profile_b_ready_is_preferred_for_unbound_task() -> None:
    resolver = RuntimeResolver(_profile_b_ready())
    selection = resolver.resolve()

    assert selection.profile == "lightweight"
    assert selection.workspace_backend == "devspace"
    assert selection.agent_backend == "local_codex_bridge"
    assert selection.status == "READY"
    assert selection.requires_user_action is False
    assert selection.fallback_allowed is True
    assert selection.delivery_backend == "github"


def test_profile_a_used_when_profile_b_is_not_ready() -> None:
    health = {
        "devspace": _health("devspace", BackendHealthStatus.UNAVAILABLE),
        "local_codex_bridge": _health("local_codex_bridge", BackendHealthStatus.UNAVAILABLE),
        "kandev": _health("kandev"),
        "control_plane": _health("control_plane"),
    }
    selection = RuntimeResolver(health).resolve()

    assert selection.profile == "existing"
    assert selection.workspace_backend == "kandev"
    assert selection.agent_backend == "control_plane"
    assert selection.status == "READY"


def test_no_profile_ready_selects_nearest_repairable_candidate() -> None:
    health = {
        "devspace": _health("devspace", BackendHealthStatus.DEGRADED),
        "local_codex_bridge": _health("local_codex_bridge", BackendHealthStatus.DEGRADED),
        "kandev": _health("kandev", BackendHealthStatus.UNAVAILABLE),
        "control_plane": _health("control_plane", BackendHealthStatus.UNAVAILABLE),
    }
    selection = RuntimeResolver(health).resolve()

    assert selection.status == "DEGRADED"
    assert selection.requires_user_action is True
    assert selection.missing == []


def test_development_style_web_first_prefers_profile_a_when_both_degraded() -> None:
    health = {
        "devspace": _health("devspace", BackendHealthStatus.DEGRADED),
        "local_codex_bridge": _health("local_codex_bridge", BackendHealthStatus.DEGRADED),
        "kandev": _health("kandev", BackendHealthStatus.DEGRADED),
        "control_plane": _health("control_plane", BackendHealthStatus.DEGRADED),
    }
    selection = RuntimeResolver(health, development_style="web_first").resolve()

    assert selection.profile == "existing"


def test_bound_task_never_silently_falls_back() -> None:
    binding = TaskBackendBinding(
        task_id="BOUND-1",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        profile="lightweight",
        bound_revision=7,
        bound_epoch=1,
    )
    health = {
        "devspace": _health("devspace", BackendHealthStatus.UNAVAILABLE),
        "local_codex_bridge": _health("local_codex_bridge", BackendHealthStatus.UNAVAILABLE),
        "kandev": _health("kandev"),
        "control_plane": _health("control_plane"),
    }
    selection = RuntimeResolver(health, task_binding=binding).resolve()

    assert selection.profile == "lightweight"
    assert selection.binding_forced is True
    assert selection.fallback_allowed is False
    assert selection.status == "UNAVAILABLE"
    assert selection.requires_user_action is True


def test_missing_capability_probe_is_reported() -> None:
    selection = RuntimeResolver(
        {
            "devspace": _health("devspace"),
            "local_codex_bridge": _health(
                "local_codex_bridge",
                BackendHealthStatus.UNAVAILABLE,
            ),
        }
    ).resolve()

    assert selection.status == "UNAVAILABLE"
    assert selection.requires_user_action is True
