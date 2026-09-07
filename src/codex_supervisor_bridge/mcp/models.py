from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from codex_supervisor_bridge.integrations.kandev_models import KandevTaskBinding
from codex_supervisor_bridge.memory.context_pack import BuiltContextPack
from codex_supervisor_bridge.memory.models import (
    Constraint,
    Decision,
    MemorySearchHit,
    Plan,
    TaskEvent,
    TaskMemory,
)


class TaskResponse(BaseModel):
    task: TaskMemory


class TaskDiscoverySummary(BaseModel):
    """Bounded provider-neutral task identity for discovery before task-scoped reads."""

    task_id: str
    title: str
    repository: str | None = None
    status: str
    phase: str
    revision: int = Field(ge=0)
    intent_version: int = Field(ge=0)
    plan_version: int = Field(ge=0)
    current_state: str | None = None
    updated_at: datetime

    @classmethod
    def from_task(cls, task: TaskMemory) -> "TaskDiscoverySummary":
        return cls(
            task_id=task.task_id,
            title=task.title,
            repository=task.repository,
            status=task.status,
            phase=task.phase.value,
            revision=task.revision,
            intent_version=task.intent_version,
            plan_version=task.plan_version,
            current_state=task.current_state,
            updated_at=task.updated_at,
        )


class TaskListResponse(BaseModel):
    tasks: list[TaskDiscoverySummary]


class ContextPackResponse(BaseModel):
    task: TaskMemory
    mode: str
    content: str
    token_estimate: int = Field(ge=0)
    truncated_sections: list[str] = Field(default_factory=list)
    over_budget_due_to_mandatory: bool = False

    @classmethod
    def from_pack(cls, pack: BuiltContextPack) -> "ContextPackResponse":
        return cls(
            task=pack.task,
            mode=pack.mode.value,
            content=pack.content,
            token_estimate=pack.token_estimate,
            truncated_sections=list(pack.truncated_sections),
            over_budget_due_to_mandatory=pack.over_budget_due_to_mandatory,
        )


class SearchResponse(BaseModel):
    task_id: str
    query: str
    hits: list[MemorySearchHit]


class TimelineResponse(BaseModel):
    task: TaskMemory
    events: list[TaskEvent]


class DecisionResponse(BaseModel):
    task: TaskMemory
    decision: Decision


class ConstraintResponse(BaseModel):
    task: TaskMemory
    constraint: Constraint


class PlanResponse(BaseModel):
    task: TaskMemory
    plan: Plan | None


class EventResponse(BaseModel):
    task: TaskMemory
    event: TaskEvent


class KandevProvisionResponse(BaseModel):
    task: TaskMemory
    binding: KandevTaskBinding
