from __future__ import annotations

from pydantic import BaseModel, Field

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
