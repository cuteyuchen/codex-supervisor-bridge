from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from codex_supervisor_bridge.memory.models import ActiveWriter


class CommandVerdict(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class CommandAuthorizationPolicy(str, Enum):
    ALLOW = "ALLOW"
    ASK = "ASK"
    DENY = "DENY"


class CommandRequest(BaseModel):
    task_id: str
    command: str = Field(min_length=1)
    cwd: Path
    workspace_root: Path
    expected_revision: int = Field(ge=0)
    current_revision: int = Field(ge=0)
    writer: ActiveWriter
    writer_epoch: int = Field(ge=1)
    approved: bool = False
    policy: CommandAuthorizationPolicy = CommandAuthorizationPolicy.ASK


class CommandAuthorization(BaseModel):
    verdict: CommandVerdict
    user_message: str
    reason: str
    audit_event: str = "COMMAND_AUTHORIZATION_CHECKED"
    dangerous: bool = False
    requires_user_action: bool = False


class CommandSessionStatus(str, Enum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    INTERRUPTED = "INTERRUPTED"
    UNKNOWN = "UNKNOWN"
    RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"


class CommandSession(BaseModel):
    task_id: str
    command_id: str
    pid: int | None = None
    status: CommandSessionStatus = CommandSessionStatus.RUNNING
    stdin_open: bool = True
    started_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    last_exit: int | None = None

    def complete(self, exit_code: int | None) -> None:
        self.status = CommandSessionStatus.COMPLETED
        self.last_exit = exit_code
        self.stdin_open = False

    def interrupt(self, *, acknowledged: bool) -> None:
        self.stdin_open = False
        self.status = CommandSessionStatus.INTERRUPTED if acknowledged else CommandSessionStatus.UNKNOWN

    def require_reconciliation(self) -> None:
        self.status = CommandSessionStatus.RECONCILIATION_REQUIRED
        self.stdin_open = False


_DANGEROUS = re.compile(
    r"(?:git\s+(?:reset\s+--hard|clean\s+-[^\s]*f)|format(?:\.com)?\b|"
    r"diskpart\b|Remove-Partition\b|reg\s+(?:delete|add)\b|"
    r"(?:npm|pip|winget|choco)\s+uninstall\b|(?:secret|credential|token).*(?:dump|export))",
    re.IGNORECASE,
)


def authorize_command(request: CommandRequest) -> CommandAuthorization:
    try:
        root = request.workspace_root.resolve()
        cwd = request.cwd.resolve()
        inside = os.path.commonpath((str(root), str(cwd))) == str(root)
    except (OSError, ValueError):
        inside = False
    if not inside:
        return CommandAuthorization(
            verdict=CommandVerdict.DENY,
            user_message="This command is outside the supervised project.",
            reason="workspace_boundary",
            requires_user_action=True,
        )
    if request.expected_revision != request.current_revision:
        return CommandAuthorization(
            verdict=CommandVerdict.DENY,
            user_message="The project changed since this command was prepared.",
            reason="stale_revision",
            requires_user_action=True,
        )
    if request.writer == ActiveWriter.NONE:
        return CommandAuthorization(
            verdict=CommandVerdict.DENY,
            user_message="No supervised writer is active for this command.",
            reason="missing_writer_lease",
            requires_user_action=True,
        )
    dangerous = bool(_DANGEROUS.search(request.command))
    if dangerous and not request.approved:
        return CommandAuthorization(
            verdict=CommandVerdict.DENY,
            user_message="This command is blocked because it can destroy data or expose credentials.",
            reason="dangerous_command",
            dangerous=True,
            requires_user_action=True,
        )
    if request.policy == CommandAuthorizationPolicy.DENY:
        return CommandAuthorization(
            verdict=CommandVerdict.DENY,
            user_message="Local command execution is disabled in Settings.",
            reason="policy_denied",
            dangerous=dangerous,
            requires_user_action=True,
        )
    if request.policy == CommandAuthorizationPolicy.ASK and not request.approved:
        return CommandAuthorization(
            verdict=CommandVerdict.ASK,
            user_message="This command needs your approval before it runs.",
            reason="policy_requires_approval",
            dangerous=dangerous,
            requires_user_action=True,
        )
    return CommandAuthorization(
        verdict=CommandVerdict.ALLOW,
        user_message="Command authorized for the supervised project.",
        reason="policy_allowed",
        dangerous=dangerous,
    )
