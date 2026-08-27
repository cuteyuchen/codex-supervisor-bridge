from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, computed_field, model_validator


class KandevCreateTaskRequest(BaseModel):
    title: str = Field(min_length=1, max_length=60)
    prompt: str | None = None
    parent_id: str | None = None
    workspace_id: str | None = None
    workflow_id: str | None = None
    workflow_step_id: str | None = None
    workspace_mode: str | None = None
    autopilot: bool = False
    agent_profile_id: str | None = None
    executor_profile_id: str | None = None
    start_agent: bool = False
    repository_id: str | None = None
    local_path: str | None = None
    repository_url: str | None = None
    base_branch: str | None = None
    external_id: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    start_when_unblocked: bool | None = None

    @model_validator(mode="after")
    def validate_repository_locator(self) -> "KandevCreateTaskRequest":
        locators = [self.repository_id, self.local_path, self.repository_url]
        if sum(bool(value) for value in locators) > 1:
            raise ValueError(
                "Pass at most one of repository_id, local_path, or repository_url"
            )
        return self

    def to_tool_arguments(self) -> dict[str, Any]:
        data = self.model_dump(exclude_none=True)
        if not data.get("blocked_by"):
            data.pop("blocked_by", None)
        return data


class KandevCapabilities(BaseModel):
    tools: list[str]
    missing_required_tools: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def compatible(self) -> bool:
        return not self.missing_required_tools


class KandevCallPayload(BaseModel):
    tool: str
    payload: dict[str, Any]


class KandevTaskBinding(BaseModel):
    supervisor_task_id: str
    kandev_task_id: str
    external_id: str
    created_or_reused: bool = True
    remote_payload: dict[str, Any] = Field(default_factory=dict)


class KandevTaskSnapshot(BaseModel):
    task_id: str
    raw: dict[str, Any]


class KandevConversationSnapshot(BaseModel):
    task_id: str
    raw: dict[str, Any]
