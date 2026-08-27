from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from .models import HealthStatus


class CodexReadiness(BaseModel):
    status: HealthStatus
    executable: str | None = None
    version: str | None = None
    process_launchable: bool = False
    authentication_ready: bool | None = None
    workspace_ready: bool = False
    user_message: str
    technical_detail: str | None = None


class CodexReadinessDetector:
    """Probe the CLI in bounded, non-mutating ways before selecting Codex."""

    def __init__(
        self,
        *,
        finder: Callable[[str], str | None] = shutil.which,
        runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    ) -> None:
        self.finder = finder
        self.runner = runner or subprocess.run

    def probe(self, *, executable: str = "codex", workspace: Path | None = None) -> CodexReadiness:
        resolved = executable if Path(executable).is_file() else self.finder(executable)
        if resolved is None:
            return CodexReadiness(
                status=HealthStatus.UNAVAILABLE,
                user_message="Codex needs installation or sign-in.",
                technical_detail="executable not found",
            )
        version = self._run([resolved, "--version"])
        if version.returncode != 0:
            return CodexReadiness(
                status=HealthStatus.DEGRADED,
                executable=resolved,
                process_launchable=False,
                user_message="Codex needs sign-in or runtime repair.",
                technical_detail="version command failed",
            )
        workspace_ready = workspace is None or (workspace.is_dir() and (workspace / ".git").exists())
        auth_ready = _auth_from_output(version.stdout, version.stderr)
        if not workspace_ready:
            status = HealthStatus.DEGRADED
            message = "Codex is ready, but the selected project is unavailable."
        elif auth_ready is not True:
            status = HealthStatus.DEGRADED
            message = "Codex needs sign-in or runtime readiness confirmation."
        else:
            status = HealthStatus.READY
            message = "Codex is ready."
        return CodexReadiness(
            status=status,
            executable=resolved,
            version=_first_line(version.stdout or version.stderr),
            process_launchable=True,
            authentication_ready=auth_ready,
            workspace_ready=workspace_ready,
            user_message=message,
            technical_detail="version command completed; authentication inferred only when CLI reports it",
        )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, "", str(exc))


def _auth_from_output(stdout: str, stderr: str) -> bool | None:
    output = f"{stdout}\n{stderr}".lower()
    if any(marker in output for marker in ("not logged in", "login required", "unauthorized")):
        return False
    if any(marker in output for marker in ("authenticated", "logged in", "ready")):
        return True
    return None


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else "unknown"
