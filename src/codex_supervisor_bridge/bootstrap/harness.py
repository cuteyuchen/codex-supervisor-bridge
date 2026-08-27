from __future__ import annotations

from enum import Enum
from typing import Callable

from pydantic import BaseModel, Field


class HarnessStep(str, Enum):
    CREATE_TASK = "create_task"
    OPEN_WORKTREE = "open_isolated_managed_worktree"
    DIRECT_READ = "direct_read"
    DIRECT_PATCH = "direct_patch"
    REVIEW = "git_diff_review"
    HANDOFF = "chatgpt_to_codex_handoff"
    CODEX_PLAN = "codex_read_only_plan"
    PLAN_APPROVAL = "supervisor_plan_approval"
    CODEX_EXECUTION = "codex_execution"
    OBSERVE = "observe"
    STEER = "steer_active_turn"
    PENDING_INTERACTION = "pending_approval_or_input"
    INTERRUPT = "interrupt"
    HANDBACK = "codex_to_chatgpt_handback"
    CHATGPT_PATCH = "chatgpt_patch"
    HARD_OVERRIDE = "hard_override"
    HARD_REPLAN = "hard_replan"
    RESTART = "restart"
    RESUME = "context_pack_resume"
    FINAL_EVIDENCE = "final_git_evidence"


class HarnessTrace(BaseModel):
    profile: str
    task_id: str
    workspace_identity: str
    steps: list[HarnessStep] = Field(default_factory=list)
    revisions: list[int] = Field(default_factory=list)
    writer_history: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)

    def semantic_signature(self) -> tuple[object, ...]:
        return (
            tuple(self.steps),
            tuple(self.revisions),
            tuple(self.writer_history),
            tuple(self.evidence),
        )


class HarnessComparison(BaseModel):
    equivalent: bool
    profile_a: HarnessTrace
    profile_b: HarnessTrace
    differences: list[str] = Field(default_factory=list)


class ProfileABHarness:
    """Run one canonical scenario against two provider implementations.

    The callbacks return normalized traces, so provider IDs and raw payloads
    cannot become part of the comparison or Supervisor memory.
    """

    def __init__(
        self,
        profile_a: Callable[[], HarnessTrace],
        profile_b: Callable[[], HarnessTrace],
    ) -> None:
        self.profile_a = profile_a
        self.profile_b = profile_b

    def run(self) -> HarnessComparison:
        trace_a = self.profile_a()
        trace_b = self.profile_b()
        differences: list[str] = []
        if trace_a.task_id != trace_b.task_id:
            differences.append("task identity changed")
        if trace_a.semantic_signature() != trace_b.semantic_signature():
            differences.append("normalized Supervisor semantics differ")
        return HarnessComparison(
            equivalent=not differences,
            profile_a=trace_a,
            profile_b=trace_b,
            differences=differences,
        )
