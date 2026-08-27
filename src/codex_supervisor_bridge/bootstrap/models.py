from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HealthStatus(str, Enum):
    READY = "READY"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    REPAIRING = "REPAIRING"


class ComponentHealth(BaseModel):
    """One capability in both the compact UX and the advanced diagnostics."""

    capability: str
    status: HealthStatus
    repairable: bool = False
    user_message: str
    recommended_action: str | None = None
    advanced: dict[str, Any] = Field(default_factory=dict)

    def user_view(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "status": self.status.value,
            "repairable": self.repairable,
            "user_message": self.user_message,
            "recommended_action": self.recommended_action,
        }

    def advanced_view(self) -> dict[str, Any]:
        return {**self.user_view(), "advanced": dict(self.advanced)}


class DoctorStatus(BaseModel):
    status: HealthStatus
    components: list[ComponentHealth] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def user_summary(self) -> list[dict[str, Any]]:
        return [item.user_view() for item in self.components]

    @property
    def advanced_diagnostics(self) -> list[dict[str, Any]]:
        return [item.advanced_view() for item in self.components]

    def component(self, capability: str) -> ComponentHealth | None:
        return next((item for item in self.components if item.capability == capability), None)


class RepairAction(BaseModel):
    action: str
    status: HealthStatus
    message: str
    requires_user_action: bool = False
    advanced: dict[str, Any] = Field(default_factory=dict)


class BootstrapStatus(BaseModel):
    status: HealthStatus
    summary: str
    project_directory: str | None = None
    selected_profile: str | None = None
    doctor: DoctorStatus
    repairs: list[RepairAction] = Field(default_factory=list)

    def user_view(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "summary": self.summary,
            "project_directory": self.project_directory,
            "components": self.doctor.user_summary,
            "repairs": [
                {
                    "action": item.action,
                    "status": item.status.value,
                    "message": item.message,
                    "requires_user_action": item.requires_user_action,
                }
                for item in self.repairs
            ],
        }

    def advanced_view(self) -> dict[str, Any]:
        return {
            **self.user_view(),
            "selected_profile": self.selected_profile,
            "diagnostics": self.doctor.advanced_diagnostics,
            "repair_details": [item.model_dump(mode="json") for item in self.repairs],
        }
