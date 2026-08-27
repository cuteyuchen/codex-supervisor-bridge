from __future__ import annotations

import json
from dataclasses import dataclass, field
from math import ceil

from .models import (
    Actor,
    ConstraintSeverity,
    ContextPackMode,
    EventType,
    SummaryType,
    TaskEvent,
    TaskMemory,
)
from .store import MemoryStore


@dataclass(frozen=True)
class BuiltContextPack:
    task: TaskMemory
    mode: ContextPackMode
    content: str
    token_estimate: int
    truncated_sections: tuple[str, ...] = field(default_factory=tuple)
    over_budget_due_to_mandatory: bool = False


class ContextPackBuilder:
    """Builds a deterministic, progressive-disclosure context for a supervisor model."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        target_chars: int = 48_000,
        hard_max_chars: int = 64_000,
    ) -> None:
        if target_chars <= 0 or hard_max_chars < target_chars:
            raise ValueError("Context budget must satisfy 0 < target_chars <= hard_max_chars")
        self.store = store
        self.target_chars = target_chars
        self.hard_max_chars = hard_max_chars

    def build(
        self,
        task_id: str,
        *,
        mode: ContextPackMode = ContextPackMode.RESUME,
        persist_snapshot: bool = False,
    ) -> BuiltContextPack:
        task = self.store.get_task(task_id)
        constraints = self.store.active_constraints(task_id)
        decisions = self.store.active_decisions(task_id)
        approved_plan = self.store.approved_plan(task_id)
        latest_plan = self.store.latest_plan(task_id)
        current_summary = self.store.latest_summary(task_id, SummaryType.CURRENT_STATE)

        mandatory: list[tuple[str, str]] = []
        optional: list[tuple[str, str]] = []

        mandatory.append(("TASK", self._task_header(task, mode)))
        mandatory.append(("USER GOAL", task.current_goal or "No explicit goal recorded."))

        hard_constraints = [
            item
            for item in constraints
            if item.severity == ConstraintSeverity.HARD
        ]
        mandatory.append(
            (
                "HARD CONSTRAINTS",
                self._bullets(
                    [f"[{item.constraint_id}] [{item.scope}] {item.content}" for item in hard_constraints],
                    empty="No active HARD constraints.",
                ),
            )
        )

        mandatory.append(
            (
                "ACTIVE DECISIONS",
                self._bullets(
                    [
                        f"[{item.decision_id}] {item.title}: {item.content}"
                        for item in decisions
                    ],
                    empty="No active decisions.",
                ),
            )
        )

        plan = latest_plan if mode == ContextPackMode.PLAN_REVIEW else approved_plan
        if plan is not None:
            label = (
                f"Plan V{plan.plan_version} / {plan.status.value} / {plan.plan_id}\n"
                f"{plan.content}"
            )
        else:
            label = "No applicable plan recorded."
        mandatory.append(("APPROVED / REVIEW PLAN", label))

        state_text = (
            current_summary.content
            if current_summary is not None
            else task.current_state or "No current-state summary recorded."
        )
        mandatory.append(("CURRENT STATE", state_text))

        soft_constraints = [
            item
            for item in constraints
            if item.severity != ConstraintSeverity.HARD
        ]
        optional.append(
            (
                "OTHER ACTIVE CONSTRAINTS",
                self._bullets(
                    [
                        f"[{item.severity.value}] [{item.constraint_id}] "
                        f"[{item.scope}] {item.content}"
                        for item in soft_constraints
                    ],
                    empty="No active SOFT/PREFERENCE constraints.",
                ),
            )
        )

        checkpoint_events = self.store.recent_events(
            task_id,
            event_types={EventType.CHECKPOINT_CREATED, EventType.CODEX_PROGRESS},
            limit=3,
        )
        optional.append(
            (
                "LATEST CODEX CHECKPOINT / PROGRESS",
                self._events(checkpoint_events, empty="No checkpoint recorded."),
            )
        )

        user_overrides = self.store.recent_events(
            task_id,
            actors={Actor.USER},
            event_types={EventType.USER_OVERRIDE, EventType.INTENT_UPDATED},
            limit=5,
        )
        optional.append(
            (
                "RECENT USER OVERRIDES",
                self._events(user_overrides, empty="No recent user override."),
            )
        )

        supervisor_events = self.store.recent_events(
            task_id,
            actors={Actor.SUPERVISOR},
            event_types={
                EventType.PLAN_APPROVED,
                EventType.PLAN_REJECTED,
                EventType.DECISION_ADDED,
                EventType.DECISION_SUPERSEDED,
                EventType.CODEX_STEERED,
            },
            limit=5,
        )
        optional.append(
            (
                "RECENT SUPERVISOR DECISIONS",
                self._events(supervisor_events, empty="No recent supervisor decision."),
            )
        )

        optional.append(("RUNTIME / GIT STATE", self._runtime_state(task)))

        evidence = self.store.recent_evidence(task_id, limit=8)
        optional.append(
            (
                "RELEVANT EVIDENCE REFERENCES",
                self._bullets(
                    [
                        f"[{item.evidence_id}] {item.evidence_type.value} via {item.source}: "
                        f"{item.summary}"
                        for item in evidence
                    ],
                    empty="No evidence indexed.",
                ),
            )
        )

        optional.append(
            (
                "CURRENT DECISION REQUIRED",
                self._decision_prompt(mode),
            )
        )

        rendered_sections = [self._section(title, body) for title, body in mandatory]
        content = "\n\n".join(rendered_sections)
        over_budget_due_to_mandatory = len(content) > self.hard_max_chars
        truncated: list[str] = []

        for title, body in optional:
            section = self._section(title, body)
            separator = "\n\n" if content else ""
            proposed = len(content) + len(separator) + len(section)
            if proposed <= self.target_chars:
                content += separator + section
                continue

            remaining = self.hard_max_chars - len(content) - len(separator)
            if remaining <= len(title) + 24:
                truncated.append(title)
                continue
            trimmed = self._section(title, self._truncate(body, remaining - len(title) - 12))
            if len(content) + len(separator) + len(trimmed) <= self.hard_max_chars:
                content += separator + trimmed
            truncated.append(title)

        token_estimate = self.estimate_tokens(content)
        pack = BuiltContextPack(
            task=task,
            mode=mode,
            content=content,
            token_estimate=token_estimate,
            truncated_sections=tuple(truncated),
            over_budget_due_to_mandatory=over_budget_due_to_mandatory,
        )
        if persist_snapshot:
            self.store.save_context_snapshot(task_id, mode, content, token_estimate)
        return pack

    @staticmethod
    def estimate_tokens(content: str) -> int:
        # Conservative dependency-free estimate. It intentionally avoids coupling
        # the durable memory layer to one model tokenizer.
        return ceil(len(content) / 4)

    @staticmethod
    def _section(title: str, body: str) -> str:
        return f"{title}\n{'-' * len(title)}\n{body.strip()}"

    @staticmethod
    def _bullets(values: list[str], *, empty: str) -> str:
        if not values:
            return empty
        return "\n".join(f"- {value}" for value in values)

    @staticmethod
    def _truncate(value: str, max_chars: int) -> str:
        if len(value) <= max_chars:
            return value
        marker = "\n...[truncated; retrieve details by evidence/search tool]"
        keep = max(0, max_chars - len(marker))
        return value[:keep].rstrip() + marker

    @staticmethod
    def _task_header(task: TaskMemory, mode: ContextPackMode) -> str:
        return "\n".join(
            [
                f"Task ID: {task.task_id}",
                f"Title: {task.title}",
                f"Repository: {task.repository or '-'}",
                f"Mode: {mode.value}",
                f"Revision: {task.revision}",
                f"Intent Version: {task.intent_version}",
                f"Plan Version: {task.plan_version}",
                f"Phase: {task.phase.value}",
            ]
        )

    @classmethod
    def _events(cls, events: list[TaskEvent], *, empty: str) -> str:
        if not events:
            return empty
        rows: list[str] = []
        for event in events:
            payload = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
            payload = cls._truncate(payload, 1200)
            rows.append(
                f"- Rev {event.revision} {event.actor.value}/{event.event_type.value}: {payload}"
            )
        return "\n".join(rows)

    @staticmethod
    def _runtime_state(task: TaskMemory) -> str:
        return "\n".join(
            [
                f"Kandev Task: {task.external_kandev_task_id or '-'}",
                f"Codex Thread: {task.codex_thread_id or '-'}",
                f"Codex Turn: {task.codex_turn_id or '-'}",
                f"Git Branch: {task.git_branch or '-'}",
                f"Git HEAD: {task.git_head or '-'}",
                f"PR: {task.pr_number if task.pr_number is not None else '-'}",
            ]
        )

    @staticmethod
    def _decision_prompt(mode: ContextPackMode) -> str:
        if mode == ContextPackMode.PLAN_REVIEW:
            return "Review the proposed plan. Decide APPROVE_PLAN or REJECT_PLAN with concrete reasons."
        if mode == ContextPackMode.CHECKPOINT_REVIEW:
            return "Decide CONTINUE, STEER, INTERRUPT, or REPLAN from the latest checkpoint."
        if mode == ContextPackMode.FINAL_REVIEW:
            return "Decide ACCEPT or CHANGES_REQUIRED against the active intent and constraints."
        if mode == ContextPackMode.DEBUG:
            return "Diagnose state consistency; do not mutate until evidence is sufficient."
        return "Resume supervision from this canonical state; do not rely on prior chat history."
