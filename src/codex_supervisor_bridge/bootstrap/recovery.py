from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from codex_supervisor_bridge.memory.models import ActiveWriter


class RecoveryStatus(str, Enum):
    READY = "READY"
    RESUME = "RESUME"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class RecoveryDecision(BaseModel):
    status: RecoveryStatus
    message: str
    requires_user_action: bool = False


class RuntimeRecovery:
    """Fail closed when durable writer ownership outlives its runtime."""

    @staticmethod
    def decide(*, active_writer: ActiveWriter, runtime_present: bool, task_state_present: bool) -> RecoveryDecision:
        if not task_state_present:
            return RecoveryDecision(
                status=RecoveryStatus.RECONCILIATION_REQUIRED,
                message="Task state is unavailable; inspect before resuming.",
                requires_user_action=True,
            )
        if active_writer == ActiveWriter.CODEX and not runtime_present:
            return RecoveryDecision(
                status=RecoveryStatus.RECONCILIATION_REQUIRED,
                message="Codex writer ownership has no live runtime; inspect before resuming.",
                requires_user_action=True,
            )
        if active_writer != ActiveWriter.NONE and runtime_present:
            return RecoveryDecision(status=RecoveryStatus.RESUME, message="Persisted task runtime can be resumed.")
        return RecoveryDecision(status=RecoveryStatus.READY, message="Task state is ready.")
