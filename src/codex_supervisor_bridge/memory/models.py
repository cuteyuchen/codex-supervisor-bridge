from __future__ import annotations

from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class Actor(str, Enum):
    USER = "USER"
    SUPERVISOR = "SUPERVISOR"
    CODEX = "CODEX"
    KANDEV = "KANDEV"
    GITHUB = "GITHUB"
    SYSTEM = "SYSTEM"


class EventType(str, Enum):
    USER_REQUEST = "USER_REQUEST"
    USER_OVERRIDE = "USER_OVERRIDE"
    INTENT_CREATED = "INTENT_CREATED"
    INTENT_UPDATED = "INTENT_UPDATED"
    PLAN_CREATED = "PLAN_CREATED"
    PLAN_APPROVED = "PLAN_APPROVED"
    PLAN_REJECTED = "PLAN_REJECTED"
    CHECKPOINT_CREATED = "CHECKPOINT_CREATED"


class TaskMemory(BaseModel):
    task_id: str
    title: str
    revision: int = 0
    intent_version: int = 1
    plan_version: int = 0
    phase: str = "created"
    created_at: datetime = Field(default_factory=datetime.utcnow)


class TaskEvent(BaseModel):
    task_id: str
    actor: Actor
    event_type: EventType
    revision: int
    payload: dict = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Decision(BaseModel):
    decision_id: str
    task_id: str
    title: str
    content: str
    status: str = "ACTIVE"


class Constraint(BaseModel):
    constraint_id: str
    task_id: str
    content: str
    severity: str = "HARD"
    status: str = "ACTIVE"
