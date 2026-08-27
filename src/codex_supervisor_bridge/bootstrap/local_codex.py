from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field

from codex_supervisor_bridge.integrations.agent_backends import LOCAL_CODEX_TOOLS

from .models import ComponentHealth, HealthStatus
from .process import ManagedProcessSpec


class LocalCodexBridgeBootstrapConfig(BaseModel):
    launch_command: list[str] = Field(min_length=1)
    version_command: list[str] = Field(default_factory=list)
    working_directory: Path | None = None
    required_tools: list[str] = Field(default_factory=lambda: sorted(LOCAL_CODEX_TOOLS))


class LocalCodexBridgeBootstrap:
    """Process and protocol checks for Local-Codex-Bridge at the provider edge."""

    def __init__(self, config: LocalCodexBridgeBootstrapConfig) -> None:
        self.config = config

    def process_spec(
        self,
        *,
        startup_timeout: float = 15.0,
        shutdown_timeout: float = 10.0,
    ) -> ManagedProcessSpec:
        return ManagedProcessSpec(
            name="local_codex_bridge",
            command=self.config.launch_command,
            cwd=self.config.working_directory,
            env=dict(os.environ),
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
        )

    def protocol_health(self, available_tools: set[str]) -> ComponentHealth:
        missing = sorted(set(self.config.required_tools) - available_tools)
        if missing:
            return ComponentHealth(
                capability="Codex control",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message="Codex control needs an update or repair.",
                recommended_action="repair_codex_control",
                advanced={"provider": "local-codex-bridge", "missing_tools": missing},
            )
        return ComponentHealth(
            capability="Codex control",
            status=HealthStatus.READY,
            user_message="Codex control is ready.",
            advanced={
                "provider": "local-codex-bridge",
                "tools": sorted(available_tools & set(self.config.required_tools)),
                "semantics": ["turn", "observe", "steer", "respond", "interrupt"],
            },
        )
