from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .lcb_hardening import LCB_HARDENING_REVISION, LCB_RUNTIME_CONTRACT
from .process import CodexProcessOwnership

logger = logging.getLogger(__name__)

RUNTIME_METADATA_VERSION = 1
INSTANCE_PREFIX = "csb-codex-"
SUPERVISOR_RUNTIME_ENV = "CODEX_SUPERVISOR_RUNTIME_INSTANCE_ID"
SUPERVISOR_EPOCH_ENV = "CODEX_SUPERVISOR_RUNTIME_EPOCH"
SUPERVISOR_TOKEN_ENV = "CODEX_SUPERVISOR_OWNERSHIP_TOKEN"
SUPERVISOR_METADATA_ENV = "CODEX_SUPERVISOR_RUNTIME_METADATA"
SUPERVISOR_PARENT_ENV = "CODEX_SUPERVISOR_PARENT_PID"
SUPERVISOR_CONTRACT_ENV = "CODEX_SUPERVISOR_RUNTIME_CONTRACT"
SUPERVISOR_RUNTIME_CONTRACT = LCB_RUNTIME_CONTRACT


class CodexRuntimeIsolationError(RuntimeError):
    """A Supervisor Codex runtime could not be proven isolated."""


class LcbRuntimeIsolationUnsupportedError(CodexRuntimeIsolationError):
    """LCB cannot be launched with a safely isolated Codex runtime."""


class RuntimeOwnershipError(CodexRuntimeIsolationError):
    """A destructive lifecycle action lacks verified ownership."""


class ProcessObservation(BaseModel):
    pid: int
    creation_time: str
    executable: str
    command_line_fingerprint: str | None = None
    parent_pid: int | None = None
    parent_creation_time: str | None = None
    parent_executable: str | None = None
    app_server_stdio: bool = False


class CodexRuntimeMetadata(BaseModel):
    schema_version: int = RUNTIME_METADATA_VERSION
    instance_id: str
    runtime_epoch: int = Field(ge=1)
    lcb_runtime_contract: str
    lcb_hardening_revision: str
    ownership: CodexProcessOwnership = CodexProcessOwnership.UNKNOWN
    ownership_token_hash: str
    status: str = "CREATED"
    runtime_directory: str
    codex_home: str
    endpoint_category: str = "stdio"
    started_at: str
    supervisor_parent_pid: int
    proxy_process: ProcessObservation | None = None
    lcb_process: ProcessObservation | None = None
    app_server_process: ProcessObservation | None = None
    desktop_processes: list[ProcessObservation] = Field(default_factory=list)
    desktop_runtime_present: bool = False
    isolation_verified: bool = False
    failure_code: str | None = None
    technical_detail: str | None = None

    def public_status(self) -> dict[str, Any]:
        """Normal UX metadata deliberately excludes PIDs and filesystem details."""

        return {
            "ownership": self.ownership.value,
            "instance_id": self.instance_id,
            "runtime_epoch": self.runtime_epoch,
            "status": self.status,
            "endpoint_category": self.endpoint_category,
            "desktop_runtime_detected": self.desktop_runtime_present,
            "isolation_verified": self.isolation_verified,
            "failure_code": self.failure_code,
        }

    def advanced_status(self) -> dict[str, Any]:
        return {
            **self.public_status(),
            "desktop_detection_code": (
                "CODEX_DESKTOP_RUNTIME_DETECTED"
                if self.desktop_runtime_present
                else None
            ),
            "runtime_directory": self.runtime_directory,
            "codex_home": self.codex_home,
            "runtime_contract": self.lcb_runtime_contract,
            "hardening_revision": self.lcb_hardening_revision,
            "proxy_process": self.proxy_process.model_dump(mode="json")
            if self.proxy_process
            else None,
            "lcb_process": self.lcb_process.model_dump(mode="json")
            if self.lcb_process
            else None,
            "app_server_process": self.app_server_process.model_dump(mode="json")
            if self.app_server_process
            else None,
            "desktop_processes": [item.model_dump(mode="json") for item in self.desktop_processes],
            "technical_detail": self.technical_detail,
        }


class ProcessInspector:
    """Read-only process inventory used only for identity and ownership checks."""

    def snapshot(self) -> list[ProcessObservation]:
        if platform.system() == "Windows":
            return self._windows_snapshot()
        return self._proc_snapshot()

    def identity(self, pid: int) -> ProcessObservation | None:
        return next((item for item in self.snapshot() if item.pid == pid), None)

    @staticmethod
    def _windows_snapshot() -> list[ProcessObservation]:
        script = r"""
$items = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | ForEach-Object {
  $commandLine = [string]$_.CommandLine
  $commandHash = ''
  if ($commandLine) {
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
      $bytes = [System.Text.Encoding]::UTF8.GetBytes($commandLine)
      $commandHash = -join ($sha256.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    } finally {
      $sha256.Dispose()
    }
  }
  $executableName = if ($_.ExecutablePath) {
    [System.IO.Path]::GetFileName([string]$_.ExecutablePath)
  } else {
    [string]$_.Name
  }
  [pscustomobject]@{
    ProcessId = [int]$_.ProcessId
    ParentProcessId = [int]$_.ParentProcessId
    CreationDate = if ($_.CreationDate) { $_.CreationDate.ToUniversalTime().ToString('o') } else { '' }
    ExecutablePath = [string]$_.ExecutablePath
    Name = [string]$_.Name
    CommandLineFingerprint = $commandHash
    AppServerStdio = (
      $executableName -in @('codex', 'codex.exe') -and
      $commandLine -match 'app-server' -and
      $commandLine -match 'stdio://'
    )
  }
}
$items | ConvertTo-Json -Compress
"""
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            payload = json.loads(completed.stdout) if completed.returncode == 0 else []
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
            return []
        rows = payload if isinstance(payload, list) else [payload]
        parents = {
            int(row["ProcessId"]): row
            for row in rows
            if isinstance(row, dict) and isinstance(row.get("ProcessId"), int)
        }
        result: list[ProcessObservation] = []
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("ProcessId"), int):
                continue
            parent = parents.get(row.get("ParentProcessId"))
            command_fingerprint = (
                row.get("CommandLineFingerprint")
                if isinstance(row.get("CommandLineFingerprint"), str)
                and row.get("CommandLineFingerprint")
                else None
            )
            executable = (
                row.get("ExecutablePath")
                if isinstance(row.get("ExecutablePath"), str) and row.get("ExecutablePath")
                else str(row.get("Name") or "")
            )
            result.append(
                ProcessObservation(
                    pid=row["ProcessId"],
                    creation_time=str(row.get("CreationDate") or "unknown"),
                    executable=executable,
                    command_line_fingerprint=command_fingerprint,
                    parent_pid=row.get("ParentProcessId")
                    if isinstance(row.get("ParentProcessId"), int)
                    else None,
                    parent_creation_time=str(parent.get("CreationDate") or "unknown")
                    if parent
                    else None,
                    parent_executable=(
                        str(parent.get("ExecutablePath") or parent.get("Name") or "")
                        if parent
                        else None
                    ),
                    app_server_stdio=bool(row.get("AppServerStdio")),
                )
            )
        return result

    @staticmethod
    def _proc_snapshot() -> list[ProcessObservation]:
        proc = Path("/proc")
        if not proc.is_dir():
            return []
        raw: dict[int, tuple[str, int | None, str, str]] = {}
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            try:
                stat = (entry / "stat").read_text(encoding="utf-8").split()
                executable = str((entry / "exe").resolve(strict=True))
                command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                    "utf-8", errors="replace"
                )
                raw[pid] = (str(stat[21]), int(stat[3]), executable, command)
            except (OSError, ValueError, IndexError):
                continue
        result: list[ProcessObservation] = []
        for pid, (created, parent_pid, executable, command) in raw.items():
            parent = raw.get(parent_pid or -1)
            result.append(
                ProcessObservation(
                    pid=pid,
                    creation_time=created,
                    executable=executable,
                    command_line_fingerprint=_fingerprint(command) if command else None,
                    parent_pid=parent_pid,
                    parent_creation_time=parent[0] if parent else None,
                    parent_executable=parent[2] if parent else None,
                    app_server_stdio=_is_stdio_app_server(executable, command),
                )
            )
        return result


class SupervisorCodexRuntimeManager:
    """Own the namespace and identity of one LCB-spawned Codex app-server.

    The app-server remains an LCB child connected over private stdio. The
    Supervisor wraps the LCB launch so the complete proxy -> LCB -> app-server
    process chain is recorded and verified before Profile B can become READY.
    """

    def __init__(
        self,
        app_data_root: str | Path,
        *,
        inspector: ProcessInspector | None = None,
        uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.app_data_root = Path(app_data_root)
        self.runtime_root = self.app_data_root / "runtime" / "codex"
        self._inspector = inspector or ProcessInspector()
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self._token: str | None = None
        self.metadata: CodexRuntimeMetadata | None = None

    @property
    def prepared(self) -> bool:
        return self.metadata is not None

    @property
    def instance_id(self) -> str | None:
        return self.metadata.instance_id if self.metadata else None

    @property
    def runtime_epoch(self) -> int:
        return self.metadata.runtime_epoch if self.metadata else 0

    @property
    def ownership(self) -> CodexProcessOwnership:
        return self.metadata.ownership if self.metadata else CodexProcessOwnership.UNKNOWN

    @property
    def isolation_verified(self) -> bool:
        return bool(self.metadata and self.metadata.isolation_verified)

    @property
    def metadata_path(self) -> Path:
        if self.metadata is None:
            raise CodexRuntimeIsolationError("Supervisor Codex runtime is not prepared")
        return Path(self.metadata.runtime_directory) / "runtime.json"

    def prepare(self, base_environment: Mapping[str, str] | None = None) -> CodexRuntimeMetadata:
        if self.metadata is not None:
            if self.metadata.failure_code == "LCB_RUNTIME_ISOLATION_UNSUPPORTED":
                raise LcbRuntimeIsolationUnsupportedError(
                    "LCB_RUNTIME_ISOLATION_UNSUPPORTED: safe runtime preparation failed"
                )
            return self.metadata
        environment = dict(os.environ if base_environment is None else base_environment)
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        epoch = self._next_epoch()
        instance_id = f"{INSTANCE_PREFIX}{self._uuid_factory()}"
        runtime_directory = self.runtime_root / instance_id
        codex_home = runtime_directory / "home"
        runtime_directory.mkdir(parents=True, exist_ok=False)
        codex_home.mkdir(parents=True, exist_ok=False)
        (runtime_directory / "lcb-checkpoints").mkdir()
        self._token = uuid.uuid4().hex
        metadata = CodexRuntimeMetadata(
            instance_id=instance_id,
            runtime_epoch=epoch,
            lcb_runtime_contract=LCB_RUNTIME_CONTRACT,
            lcb_hardening_revision=LCB_HARDENING_REVISION,
            ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
            ownership_token_hash=_fingerprint(self._token),
            runtime_directory=str(runtime_directory),
            codex_home=str(codex_home),
            started_at=self._clock().isoformat(),
            supervisor_parent_pid=os.getpid(),
        )
        self.metadata = metadata
        source_home = _source_codex_home(environment)
        try:
            self._seed_compatibility_layer(source_home, codex_home)
        except LcbRuntimeIsolationUnsupportedError:
            self.metadata = metadata.model_copy(
                update={
                    "status": "DEGRADED",
                    "isolation_verified": False,
                    "failure_code": "LCB_RUNTIME_ISOLATION_UNSUPPORTED",
                    "technical_detail": "safe provider compatibility overlay failed",
                }
            )
            self._write_metadata(self.metadata)
            raise
        self._write_metadata(metadata)
        logger.info(
            "runtime instance created instance_id=%s epoch=%s",
            metadata.instance_id,
            metadata.runtime_epoch,
        )
        return metadata

    def replace(self, base_environment: Mapping[str, str] | None = None) -> CodexRuntimeMetadata:
        """Advance the runtime epoch without reusing any thread/session namespace."""

        if self.metadata is not None:
            current = self.refresh()
            self.metadata = current.model_copy(
                update={
                    "status": "REPLACED",
                    "isolation_verified": False,
                    "technical_detail": "Supervisor runtime replaced by a new epoch",
                }
            )
            self._write_metadata(self.metadata)
            logger.info(
                "runtime replaced instance_id=%s epoch=%s",
                current.instance_id,
                current.runtime_epoch,
            )
        self.metadata = None
        self._token = None
        return self.prepare(base_environment)

    def environment(self, base_environment: Mapping[str, str] | None = None) -> dict[str, str]:
        metadata = self.metadata or self.prepare(base_environment)
        if self._token is None:
            raise CodexRuntimeIsolationError("runtime ownership token is unavailable")
        environment = dict(os.environ if base_environment is None else base_environment)
        environment["CODEX_HOME"] = metadata.codex_home
        environment["LOCAL_CODEX_BRIDGE_CHECKPOINT_DIR"] = str(
            Path(metadata.runtime_directory) / "lcb-checkpoints"
        )
        environment[SUPERVISOR_RUNTIME_ENV] = metadata.instance_id
        environment[SUPERVISOR_EPOCH_ENV] = str(metadata.runtime_epoch)
        environment[SUPERVISOR_TOKEN_ENV] = self._token
        environment[SUPERVISOR_METADATA_ENV] = str(self.metadata_path)
        environment[SUPERVISOR_PARENT_ENV] = str(os.getpid())
        environment[SUPERVISOR_CONTRACT_ENV] = SUPERVISOR_RUNTIME_CONTRACT
        return environment

    def wrapped_lcb_command(self, launch_command: Sequence[str]) -> list[str]:
        if not launch_command or not str(launch_command[0]).strip():
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: empty LCB launch command"
            )
        if self.metadata is None:
            raise CodexRuntimeIsolationError("Supervisor Codex runtime is not prepared")
        return [
            sys.executable,
            "-m",
            "codex_supervisor_bridge.bootstrap.lcb_runtime_proxy",
            "--metadata",
            str(self.metadata_path),
            "--",
            *[str(item) for item in launch_command],
        ]

    def refresh(self) -> CodexRuntimeMetadata:
        if self.metadata is None:
            raise CodexRuntimeIsolationError("Supervisor Codex runtime is not prepared")
        try:
            payload = json.loads(self.metadata_path.read_text(encoding="utf-8"))
            observed = CodexRuntimeMetadata.model_validate(payload)
        except (OSError, ValueError, TypeError) as exc:
            return self._fail("CODEX_RUNTIME_OWNERSHIP_UNKNOWN", type(exc).__name__)
        if (
            observed.instance_id != self.metadata.instance_id
            or observed.runtime_epoch != self.metadata.runtime_epoch
            or observed.lcb_runtime_contract != self.metadata.lcb_runtime_contract
            or observed.lcb_hardening_revision != self.metadata.lcb_hardening_revision
            or observed.ownership_token_hash != self.metadata.ownership_token_hash
            or observed.runtime_directory != self.metadata.runtime_directory
            or observed.codex_home != self.metadata.codex_home
            or observed.endpoint_category != self.metadata.endpoint_category
            or observed.started_at != self.metadata.started_at
            or observed.supervisor_parent_pid != self.metadata.supervisor_parent_pid
        ):
            return self._fail(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                "runtime metadata identity/token mismatch",
            )
        self.metadata = observed
        return observed

    def wait_until_verified(self, timeout: float = 15.0) -> CodexRuntimeMetadata:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            metadata = self.refresh()
            if metadata.isolation_verified:
                logger.info(
                    "runtime ownership verified instance_id=%s epoch=%s",
                    metadata.instance_id,
                    metadata.runtime_epoch,
                )
                return metadata
            if metadata.failure_code:
                raise CodexRuntimeIsolationError(
                    f"{metadata.failure_code}: {metadata.technical_detail or 'runtime verification failed'}"
                )
            if time.monotonic() >= deadline:
                failed = self._fail(
                    "SUPERVISOR_CODEX_RUNTIME_FAILED",
                    "timed out waiting for verified proxy -> LCB -> app-server ownership",
                )
                raise CodexRuntimeIsolationError(
                    f"{failed.failure_code}: {failed.technical_detail}"
                )
            self._sleep(0.05)

    def verify_metadata(self, metadata: CodexRuntimeMetadata) -> CodexRuntimeMetadata:
        """Validate fake or live metadata without performing a lifecycle action."""

        reason = runtime_verification_failure(metadata)
        runtime_directory = Path(metadata.runtime_directory)
        if reason is None and runtime_directory.parent != self.runtime_root:
            reason = "Supervisor runtime directory is outside the canonical runtime root"
        if reason:
            return metadata.model_copy(
                update={
                    "status": "DEGRADED",
                    "isolation_verified": False,
                    "failure_code": "UNSAFE_SHARED_CODEX_RUNTIME"
                    if "UNSAFE_SHARED" in reason
                    else "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                    "technical_detail": reason,
                }
            )
        return metadata.model_copy(
            update={
                "status": "READY",
                "isolation_verified": True,
                "failure_code": None,
                "technical_detail": "Supervisor-owned stdio runtime ownership verified",
            }
        )

    def assert_destructive_lifecycle_allowed(self) -> None:
        metadata = self.refresh()
        if metadata.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: destructive lifecycle refused"
            )
        if self._token is None or _fingerprint(self._token) != metadata.ownership_token_hash:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: ownership token mismatch"
            )
        if runtime_verification_failure(metadata) is not None:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: persisted process chain is not verified"
            )
        processes = {item.pid: item for item in self._inspector.snapshot()}
        for expected in (metadata.proxy_process, metadata.lcb_process):
            if expected is None:
                continue
            current = processes.get(expected.pid)
            if current is None:
                continue
            if not _same_observation_identity(expected, current):
                raise RuntimeOwnershipError(
                    "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: process identity changed"
                )

    def mark_degraded(self, code: str, detail: str) -> CodexRuntimeMetadata:
        return self._fail(code, detail)

    def mark_stopped(self) -> CodexRuntimeMetadata | None:
        if self.metadata is None:
            return None
        metadata = self.refresh()
        self.metadata = metadata.model_copy(
            update={"status": "STOPPED", "isolation_verified": False}
        )
        self._write_metadata(self.metadata)
        logger.info(
            "runtime stopped instance_id=%s epoch=%s",
            self.metadata.instance_id,
            self.metadata.runtime_epoch,
        )
        return self.metadata

    def public_status(self) -> dict[str, Any]:
        if self.metadata is None:
            return {
                "ownership": CodexProcessOwnership.UNKNOWN.value,
                "instance_id": None,
                "runtime_epoch": 0,
                "status": "NOT_STARTED",
                "desktop_runtime_detected": False,
                "isolation_verified": False,
            }
        return self.refresh().public_status()

    def advanced_status(self) -> dict[str, Any]:
        return self.refresh().advanced_status() if self.metadata else self.public_status()

    def _next_epoch(self) -> int:
        path = self.runtime_root / "epoch.json"
        current = 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            current = int(payload.get("epoch", 0)) if isinstance(payload, dict) else 0
        except (OSError, ValueError, TypeError):
            current = 0
        epoch = max(0, current) + 1
        _atomic_json(path, {"epoch": epoch})
        return epoch

    def _seed_compatibility_layer(self, source_home: Path, target_home: Path) -> None:
        source_config = source_home / "config.toml"
        if source_config.is_file():
            try:
                rendered = _render_safe_codex_config_file(source_config)
            except (OSError, UnicodeError, tomllib.TOMLDecodeError, ValueError) as exc:
                raise LcbRuntimeIsolationUnsupportedError(
                    "LCB_RUNTIME_ISOLATION_UNSUPPORTED: safe provider config overlay "
                    f"could not be created ({type(exc).__name__})"
                ) from exc
            (target_home / "config.toml").write_text(rendered, encoding="utf-8")

    def _fail(self, code: str, detail: str) -> CodexRuntimeMetadata:
        if self.metadata is None:
            raise CodexRuntimeIsolationError(f"{code}: {detail}")
        ownership = (
            CodexProcessOwnership.UNKNOWN
            if code in {
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                "UNSAFE_SHARED_CODEX_RUNTIME",
            }
            else self.metadata.ownership
        )
        self.metadata = self.metadata.model_copy(
            update={
                "status": "DEGRADED",
                "ownership": ownership,
                "isolation_verified": False,
                "failure_code": code,
                "technical_detail": detail,
            }
        )
        self._write_metadata(self.metadata)
        return self.metadata

    def _write_metadata(self, metadata: CodexRuntimeMetadata) -> None:
        _atomic_json(self.metadata_path, metadata.model_dump(mode="json"))


def _source_codex_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def runtime_verification_failure(metadata: CodexRuntimeMetadata) -> str | None:
    desktop_pids = {item.pid for item in metadata.desktop_processes}
    proxy = metadata.proxy_process
    lcb = metadata.lcb_process
    app_server = metadata.app_server_process
    runtime_directory = Path(metadata.runtime_directory)
    codex_home = Path(metadata.codex_home)
    if metadata.lcb_runtime_contract != LCB_RUNTIME_CONTRACT:
        return "LCB runtime contract is unsupported"
    if metadata.lcb_hardening_revision != LCB_HARDENING_REVISION:
        return "LCB lifecycle hardening revision is unsupported"
    if metadata.endpoint_category != "stdio":
        return "Supervisor runtime endpoint is not private stdio"
    if metadata.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
        return "runtime ownership is not SUPERVISOR_MANAGED"
    if proxy is None or lcb is None or app_server is None:
        return "process chain metadata is incomplete"
    if len({proxy.pid, lcb.pid, app_server.pid}) != 3:
        return "Supervisor runtime process identities are not distinct"
    for label, process in (
        ("proxy", proxy),
        ("LCB", lcb),
        ("Codex app-server", app_server),
    ):
        if not _observation_identity_complete(process):
            return f"{label} process identity is incomplete"
    if proxy.parent_pid != metadata.supervisor_parent_pid:
        return "runtime proxy is not a child of the Supervisor process"
    if not _parent_identity_matches(lcb, proxy):
        return "LCB parent identity does not match the Supervisor runtime proxy"
    if not _parent_identity_matches(app_server, lcb):
        return "Codex app-server parent identity does not match the owned LCB process"
    if not app_server.app_server_stdio:
        return "Codex child is not an app-server stdio instance"
    if app_server.pid in desktop_pids:
        return "UNSAFE_SHARED_CODEX_RUNTIME: Supervisor app-server matches Desktop PID"
    if codex_home != runtime_directory / "home":
        return "Codex home is outside the Supervisor runtime namespace"
    if runtime_directory.name != metadata.instance_id:
        return "runtime directory does not match the Supervisor instance identity"
    return None


def runtime_process_chain_failure(
    metadata: CodexRuntimeMetadata,
    processes: Sequence[ProcessObservation],
) -> str | None:
    reason = runtime_verification_failure(metadata)
    if reason is not None:
        return reason
    current_by_pid = {item.pid: item for item in processes}
    for label, expected in (
        ("runtime proxy", metadata.proxy_process),
        ("LCB", metadata.lcb_process),
        ("Codex app-server", metadata.app_server_process),
    ):
        if expected is None:
            return f"{label} process identity is missing"
        current = current_by_pid.get(expected.pid)
        if current is None:
            return f"{label} process is not running"
        if not _same_observation_identity(expected, current):
            return f"{label} process identity changed"
    return None


def _observation_identity_complete(process: ProcessObservation) -> bool:
    return bool(
        process.pid > 0
        and process.creation_time
        and process.creation_time != "unknown"
        and process.executable
        and process.command_line_fingerprint
        and process.parent_pid is not None
        and process.parent_creation_time
        and process.parent_creation_time != "unknown"
        and process.parent_executable
    )


def _parent_identity_matches(
    child: ProcessObservation,
    parent: ProcessObservation,
) -> bool:
    return bool(
        child.parent_pid == parent.pid
        and child.parent_creation_time == parent.creation_time
        and _same_executable(child.parent_executable, parent.executable)
    )


def _same_observation_identity(
    expected: ProcessObservation,
    current: ProcessObservation,
) -> bool:
    return bool(
        expected.pid == current.pid
        and expected.creation_time == current.creation_time
        and _same_executable(expected.executable, current.executable)
        and expected.command_line_fingerprint == current.command_line_fingerprint
        and expected.parent_pid == current.parent_pid
        and expected.parent_creation_time == current.parent_creation_time
        and _same_executable(expected.parent_executable, current.parent_executable)
    )


def _same_executable(left: str | None, right: str | None) -> bool:
    if not left or not right:
        return False
    return os.path.normcase(left) == os.path.normcase(right)


def _render_safe_codex_config_file(source: Path) -> str:
    """Parse only allowlisted provider fields, never secret-bearing sections."""

    allowed_root = frozenset(
        {
            "model",
            "model_provider",
            "model_reasoning_effort",
            "model_reasoning_summary",
            "model_verbosity",
            "service_tier",
            "web_search",
        }
    )
    allowed_provider = frozenset(
        {
            "name",
            "base_url",
            "wire_api",
            "requires_openai_auth",
            "requires_openai_account",
            "env_key",
            "request_max_retries",
            "stream_max_retries",
            "stream_idle_timeout_ms",
        }
    )
    root_values: dict[str, Any] = {}
    provider_values: dict[str, dict[str, Any]] = {}
    section: tuple[str, str | None] = ("root", None)
    with source.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            if stripped.startswith("["):
                provider_name = _provider_section_name(stripped)
                section = (
                    ("provider", provider_name)
                    if provider_name is not None
                    else ("ignored", None)
                )
                continue
            key = _bare_assignment_key(raw_line)
            if key is None:
                continue
            if section[0] == "root" and key in allowed_root:
                root_values[key] = _parse_single_assignment(raw_line, key)
            elif section[0] == "provider" and key in allowed_provider:
                provider_name = section[1]
                if provider_name is None:
                    raise ValueError("provider section identity is missing")
                value = _parse_single_assignment(raw_line, key)
                provider_values.setdefault(provider_name, {})[key] = _safe_provider_value(
                    key,
                    value,
                )

    output: list[str] = []
    for key in sorted(root_values):
        output.append(f"{key} = {_toml_value(root_values[key])}")
    for provider_name in sorted(provider_values):
        output.append("")
        output.append(f"[model_providers.{_toml_key(provider_name)}]")
        for key in sorted(provider_values[provider_name]):
            output.append(f"{key} = {_toml_value(provider_values[provider_name][key])}")
    if not output:
        return "# Supervisor runtime provider overlay intentionally contains no user MCP state.\n"
    return "\n".join(output).rstrip() + "\n"


def _bare_assignment_key(line: str) -> str | None:
    match = re.match(r"^\s*([A-Za-z0-9_-]+)\s*=", line)
    return match.group(1) if match else None


def _parse_single_assignment(line: str, expected_key: str) -> Any:
    parsed = tomllib.loads(line)
    if set(parsed) != {expected_key}:
        raise ValueError(f"invalid allowlisted Codex config field: {expected_key}")
    return parsed[expected_key]


def _provider_section_name(line: str) -> str | None:
    parsed = tomllib.loads(f"{line}\n__csb_probe = true\n")
    providers = parsed.get("model_providers")
    if providers is None:
        return None
    if not isinstance(providers, Mapping) or len(providers) != 1:
        return None
    provider_name, provider = next(iter(providers.items()))
    if (
        not isinstance(provider_name, str)
        or not isinstance(provider, Mapping)
        or provider.get("__csb_probe") is not True
    ):
        return None
    return provider_name


def _toml_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return value
    return json.dumps(value, ensure_ascii=False)


def _safe_provider_value(key: str, value: Any) -> Any:
    if key == "base_url" and isinstance(value, str):
        parsed = urlsplit(value)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider base_url contains credential-bearing components")
    return value


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise ValueError(f"unsupported provider config value type: {type(value).__name__}")


def _is_stdio_app_server(executable: str, command_line: str) -> bool:
    name = Path(executable).name.casefold()
    lowered = command_line.casefold()
    return (
        name in {"codex", "codex.exe"}
        and "app-server" in lowered
        and "stdio://" in lowered
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


__all__ = [
    "CodexProcessOwnership",
    "CodexRuntimeIsolationError",
    "CodexRuntimeMetadata",
    "LcbRuntimeIsolationUnsupportedError",
    "ProcessInspector",
    "ProcessObservation",
    "RuntimeOwnershipError",
    "SupervisorCodexRuntimeManager",
    "runtime_process_chain_failure",
    "runtime_verification_failure",
]
