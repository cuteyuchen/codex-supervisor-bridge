from __future__ import annotations

from dataclasses import dataclass

from .models import Constraint, Decision, TaskMemory


@dataclass
class ContextPack:
    task: TaskMemory
    constraints: list[Constraint]
    decisions: list[Decision]
    current_state: str

    def render(self) -> str:
        lines = [
            "SUPERVISED TASK CONTEXT",
            "",
            f"Task: {self.task.task_id}",
            f"Revision: {self.task.revision}",
            f"Intent Version: {self.task.intent_version}",
            f"Plan Version: {self.task.plan_version}",
            "",
            "HARD CONSTRAINTS",
        ]

        for item in self.constraints:
            if item.status == "ACTIVE":
                lines.append(f"- [{item.severity}] {item.content}")

        lines.extend(["", "ACTIVE DECISIONS"])
        for item in self.decisions:
            if item.status == "ACTIVE":
                lines.append(f"- {item.title}: {item.content}")

        lines.extend([
            "",
            "CURRENT STATE",
            self.current_state,
        ])

        return "\n".join(lines)
