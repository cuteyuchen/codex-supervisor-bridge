from __future__ import annotations

import hashlib
import json
import logging
import ntpath
import os
import platform
import re
import subprocess
import sys
import time
import tomllib
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from pydantic import BaseModel, Field

from .codex_runtime import CodexExecutableResolver
from .lcb_hardening import LCB_HARDENING_REVISION, LCB_RUNTIME_CONTRACT
from .physical import PhysicalPathGuard, PhysicalPathVerificationError
from .process import CodexProcessOwnership

logger = logging.getLogger(__name__)

RUNTIME_METADATA_VERSION = 3
INSTANCE_PREFIX = "csb-codex-"
SUPERVISOR_RUNTIME_ENV = "CODEX_SUPERVISOR_RUNTIME_INSTANCE_ID"
SUPERVISOR_EPOCH_ENV = "CODEX_SUPERVISOR_RUNTIME_EPOCH"
SUPERVISOR_TOKEN_ENV = "CODEX_SUPERVISOR_OWNERSHIP_TOKEN"
SUPERVISOR_METADATA_ENV = "CODEX_SUPERVISOR_RUNTIME_METADATA"
SUPERVISOR_PARENT_ENV = "CODEX_SUPERVISOR_PARENT_PID"
SUPERVISOR_CONTRACT_ENV = "CODEX_SUPERVISOR_RUNTIME_CONTRACT"
SUPERVISOR_HOST_INSTANCE_ENV = "CODEX_SUPERVISOR_HOST_INSTANCE_ID"
SUPERVISOR_RUNTIME_CONTRACT = LCB_RUNTIME_CONTRACT


class CodexRuntimeIsolationError(RuntimeError):
    """A Supervisor Codex runtime could not be proven isolated."""


class LcbRuntimeIsolationUnsupportedError(CodexRuntimeIsolationError):
    """LCB cannot be launched with a safely isolated Codex runtime."""


class RuntimeOwnershipError(CodexRuntimeIsolationError):
    """A destructive lifecycle action lacks verified ownership."""


class ProxyLaunchMode(StrEnum):
    DIRECT = "DIRECT"
    WINDOWS_VENV_TRAMPOLINE = "WINDOWS_VENV_TRAMPOLINE"


class ProcessObservation(BaseModel):
    pid: int
    creation_time: str
    executable: str
    command_line_fingerprint: str | None = None
    parent_pid: int | None = None
    parent_creation_time: str | None = None
    parent_executable: str | None = None
    app_server_stdio: bool = False


@dataclass(frozen=True, slots=True)
class ProcessSnapshotIndex:
    """Read-only PID index bound to one captured process snapshot."""

    by_pid: Mapping[int, ProcessObservation]

    @classmethod
    def from_observations(
        cls,
        observations: Sequence[ProcessObservation],
    ) -> "ProcessSnapshotIndex":
        return cls(MappingProxyType({item.pid: item for item in observations}))

    def get(self, pid: int) -> ProcessObservation | None:
        return self.by_pid.get(pid)


@dataclass(frozen=True, slots=True)
class ProxyLaunchProvenance:
    verified: bool
    mode: ProxyLaunchMode | None = None
    proxy_process: ProcessObservation | None = None
    launcher_process: ProcessObservation | None = None
    failure_reason: str | None = None


@dataclass(frozen=True, slots=True)
class _WindowsVenvLaunchSpec:
    launcher_executable: str
    base_executable: str


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
    codex_executable: str | None = None
    endpoint_category: str = "stdio"
    started_at: str
    supervisor_parent_pid: int
    supervisor_host_instance_id: str | None = None
    supervisor_parent_process: ProcessObservation | None = None
    proxy_launch_mode: ProxyLaunchMode | None = None
    proxy_launcher_process: ProcessObservation | None = None
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
            "codex_executable": self.codex_executable,
            "supervisor_host_instance_id": self.supervisor_host_instance_id,
            "supervisor_parent_process": self.supervisor_parent_process.model_dump(mode="json")
            if self.supervisor_parent_process
            else None,
            "runtime_contract": self.lcb_runtime_contract,
            "hardening_revision": self.lcb_hardening_revision,
            "proxy_launch_mode": self.proxy_launch_mode.value
            if self.proxy_launch_mode
            else None,
            "proxy_launcher_process": self.proxy_launcher_process.model_dump(mode="json")
            if self.proxy_launcher_process
            else None,
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
        path_guard: PhysicalPathGuard | None = None,
        executable_resolver: CodexExecutableResolver | None = None,
        host: object | None = None,
    ) -> None:
        self.app_data_root = Path(app_data_root)
        self.runtime_root = self.app_data_root / "runtime" / "codex"
        self._inspector = inspector or ProcessInspector()
        self._uuid_factory = uuid_factory
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._sleep = sleep
        self.path_guard = path_guard or PhysicalPathGuard()
        self.executable_resolver = executable_resolver
        # Production composition supplies the StandaloneSupervisorHost. The
        # host proof is deliberately optional for portable fake-runtime tests.
        self._host = host
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
        self.path_guard.verify_root(self.app_data_root, role="app_data")
        host_instance_id = self._verify_standalone_host()
        resolver = self.executable_resolver or CodexExecutableResolver(
            environ=environment,
            path_guard=self.path_guard,
        )
        executable_candidate = resolver.resolve()
        if not executable_candidate.exists or not executable_candidate.path:
            detail = executable_candidate.technical_detail or (
                "Codex executable could not be resolved from configured, "
                "Desktop bundled, or PATH candidates"
            )
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: " + detail
            )
        codex_executable = executable_candidate.path
        self.path_guard.verify_root(Path(codex_executable), role="process")
        supervisor_parent_process = self._inspector.identity(os.getpid())
        supervisor_host_instance_id = (
            host_instance_id
            or environment.get(SUPERVISOR_HOST_INSTANCE_ENV, "").strip()
            or None
        )
        self.path_guard.ensure_directory(self.runtime_root, role="runtime")
        epoch = self._next_epoch()
        instance_id = f"{INSTANCE_PREFIX}{self._uuid_factory()}"
        runtime_directory = self.runtime_root / instance_id
        codex_home = runtime_directory / "home"
        self.path_guard.ensure_directory(runtime_directory, role="runtime")
        self.path_guard.ensure_directory(codex_home, role="codex_home")
        checkpoints = runtime_directory / "lcb-checkpoints"
        self.path_guard.ensure_directory(checkpoints, role="runtime")
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
            codex_executable=codex_executable,
            started_at=self._clock().isoformat(),
            supervisor_parent_pid=(
                supervisor_parent_process.pid
                if supervisor_parent_process is not None
                else os.getpid()
            ),
            supervisor_host_instance_id=supervisor_host_instance_id,
            supervisor_parent_process=supervisor_parent_process,
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
        if metadata.codex_executable:
            environment["CODEX_EXE"] = metadata.codex_executable
        environment["LOCAL_CODEX_BRIDGE_CHECKPOINT_DIR"] = str(
            Path(metadata.runtime_directory) / "lcb-checkpoints"
        )
        environment[SUPERVISOR_RUNTIME_ENV] = metadata.instance_id
        environment[SUPERVISOR_EPOCH_ENV] = str(metadata.runtime_epoch)
        environment[SUPERVISOR_TOKEN_ENV] = self._token
        environment[SUPERVISOR_METADATA_ENV] = str(self.metadata_path)
        environment[SUPERVISOR_PARENT_ENV] = str(os.getpid())
        if metadata.supervisor_host_instance_id:
            environment[SUPERVISOR_HOST_INSTANCE_ENV] = metadata.supervisor_host_instance_id
        environment[SUPERVISOR_CONTRACT_ENV] = SUPERVISOR_RUNTIME_CONTRACT
        return environment

    def wrapped_lcb_command(self, launch_command: Sequence[str]) -> list[str]:
        if not launch_command or not str(launch_command[0]).strip():
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: empty LCB launch command"
            )
        if self.metadata is None:
            raise CodexRuntimeIsolationError("Supervisor Codex runtime is not prepared")
        spec, failure = _windows_venv_launch_spec()
        if failure is not None and _windows_venv_active():
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: " + failure
            )
        if spec is not None:
            path_failure = _proxy_launch_path_failure(spec, self.path_guard)
            if path_failure is not None:
                raise LcbRuntimeIsolationUnsupportedError(
                    "LCB_RUNTIME_ISOLATION_UNSUPPORTED: " + path_failure
                )
        return [
            sys.executable,
            "-m",
            "codex_supervisor_bridge.bootstrap.lcb_runtime_proxy",
            "--metadata",
            str(self.metadata_path),
            "--",
            *[str(item) for item in launch_command],
        ]

    def refresh(self, *, verify_live: bool = True) -> CodexRuntimeMetadata:
        if self.metadata is None:
            raise CodexRuntimeIsolationError("Supervisor Codex runtime is not prepared")
        metadata_path = self.metadata_path
        try:
            self.path_guard.verify_root(
                self.runtime_root,
                role="runtime",
                require_directory=True,
            )
            runtime_directory = Path(self.metadata.runtime_directory)
            if runtime_directory.parent != self.runtime_root:
                return self._fail(
                    "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                    "runtime metadata namespace is outside the canonical runtime root",
                    persist=False,
                )
            self.path_guard.verify_subpath(
                runtime_directory,
                self.runtime_root,
                role="runtime",
                require_directory=True,
            )
            self.path_guard.verify_root(metadata_path, role="runtime")
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            observed = CodexRuntimeMetadata.model_validate(payload)
        except PhysicalPathVerificationError as exc:
            return self._fail(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                f"runtime metadata physical path verification failed: {exc.code}",
                persist=False,
            )
        except (OSError, ValueError, TypeError) as exc:
            return self._fail("CODEX_RUNTIME_OWNERSHIP_UNKNOWN", type(exc).__name__)
        immutable_identity_changed = (
            observed.schema_version != self.metadata.schema_version
            or observed.instance_id != self.metadata.instance_id
            or observed.runtime_epoch != self.metadata.runtime_epoch
            or observed.lcb_runtime_contract != self.metadata.lcb_runtime_contract
            or observed.lcb_hardening_revision != self.metadata.lcb_hardening_revision
            or observed.ownership_token_hash != self.metadata.ownership_token_hash
            or observed.runtime_directory != self.metadata.runtime_directory
            or observed.codex_home != self.metadata.codex_home
            or observed.codex_executable != self.metadata.codex_executable
            or observed.endpoint_category != self.metadata.endpoint_category
            or observed.started_at != self.metadata.started_at
            or observed.supervisor_parent_pid != self.metadata.supervisor_parent_pid
            or observed.supervisor_host_instance_id
            != self.metadata.supervisor_host_instance_id
            or observed.supervisor_parent_process != self.metadata.supervisor_parent_process
        )
        trusted_chain_changed = bool(
            self.metadata.proxy_process is not None
            and (
                observed.proxy_launch_mode != self.metadata.proxy_launch_mode
                or observed.proxy_launcher_process != self.metadata.proxy_launcher_process
                or observed.proxy_process != self.metadata.proxy_process
                or observed.lcb_process != self.metadata.lcb_process
                or observed.app_server_process != self.metadata.app_server_process
            )
        )
        if immutable_identity_changed or trusted_chain_changed:
            return self._fail(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                "runtime metadata identity/token mismatch",
            )
        self.metadata = observed
        if verify_live and observed.isolation_verified:
            live_failure = runtime_process_chain_failure(
                observed,
                self._inspector.snapshot(),
                path_guard=self.path_guard,
            )
            if live_failure is not None:
                return self._fail(
                    "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                    live_failure,
                )
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

        reason = runtime_verification_failure(metadata, path_guard=self.path_guard)
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
        self.path_guard.verify_root(self.runtime_root, role="runtime", require_directory=True)
        metadata = self.refresh(verify_live=False)
        if metadata.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: "
                + (metadata.technical_detail or "destructive lifecycle refused")
            )
        if self._token is None or _fingerprint(self._token) != metadata.ownership_token_hash:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: ownership token mismatch"
            )
        if runtime_verification_failure(metadata, path_guard=self.path_guard) is not None:
            raise RuntimeOwnershipError(
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN: persisted process chain is not verified"
            )
        live_failure = runtime_process_chain_failure(
            metadata,
            self._inspector.snapshot(),
            path_guard=self.path_guard,
        )
        if live_failure is not None:
            raise RuntimeOwnershipError(
                f"CODEX_RUNTIME_OWNERSHIP_UNKNOWN: {live_failure}"
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
        self.path_guard.verify_root(
            self.runtime_root,
            role="runtime",
            require_directory=True,
        )
        if path.exists():
            self.path_guard.verify_subpath(path, self.runtime_root, role="runtime")
        current = 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            current = int(payload.get("epoch", 0)) if isinstance(payload, dict) else 0
        except (OSError, ValueError, TypeError):
            current = 0
        epoch = max(0, current) + 1
        _atomic_json(path, {"epoch": epoch}, path_guard=self.path_guard)
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
            target = target_home / "config.toml"
            _atomic_text(target, rendered, path_guard=self.path_guard, role="codex_home")

    def _fail(
        self,
        code: str,
        detail: str,
        *,
        persist: bool = True,
    ) -> CodexRuntimeMetadata:
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
        if persist:
            self._write_metadata(self.metadata)
        return self.metadata

    def _write_metadata(self, metadata: CodexRuntimeMetadata) -> None:
        _atomic_json(
            self.metadata_path,
            metadata.model_dump(mode="json"),
            path_guard=self.path_guard,
        )

    def _verify_standalone_host(self) -> str | None:
        """Require the real Standalone Host proof for production composition."""

        if self._host is None:
            return None
        try:
            evidence = self._host.assert_ready()  # type: ignore[attr-defined]
            identity = self._host.ensure_identity(evidence=evidence)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - fail-closed host boundary
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: Standalone Supervisor Host "
                f"proof failed ({type(exc).__name__})"
            ) from exc
        if not getattr(evidence, "physical_root_verified", False):
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: Supervisor Host physical root "
                "is not verified"
            )
        ownership = getattr(getattr(identity, "ownership", None), "value", None)
        if ownership != "SUPERVISOR_HOST_MANAGED":
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: Supervisor Host ownership is "
                "not SUPERVISOR_HOST_MANAGED"
            )
        if getattr(identity, "pid", None) != os.getpid():
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: Supervisor Host PID does not "
                "match the runtime owner"
            )
        instance_id = getattr(identity, "host_instance_id", None)
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise LcbRuntimeIsolationUnsupportedError(
                "LCB_RUNTIME_ISOLATION_UNSUPPORTED: Supervisor Host instance "
                "identity is missing"
            )
        return instance_id.strip()


def _source_codex_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("CODEX_HOME", "").strip()
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def proxy_launch_provenance(
    persisted_host: ProcessObservation,
    proxy_pid: int,
    process_snapshot: ProcessSnapshotIndex,
    *,
    expected_proxy: ProcessObservation | None = None,
    expected_mode: ProxyLaunchMode | None = None,
    expected_launcher: ProcessObservation | None = None,
    path_guard: PhysicalPathGuard | None = None,
) -> ProxyLaunchProvenance:
    """Verify Host -> [one exact Windows venv launcher] -> proxy ownership."""

    def failed(
        reason: str,
        *,
        proxy: ProcessObservation | None = None,
        launcher: ProcessObservation | None = None,
    ) -> ProxyLaunchProvenance:
        return ProxyLaunchProvenance(
            verified=False,
            proxy_process=proxy,
            launcher_process=launcher,
            failure_reason=reason,
        )

    if not _observation_identity_complete(persisted_host):
        return failed("persisted Supervisor Host process identity is incomplete")
    current_host = process_snapshot.get(persisted_host.pid)
    if current_host is None:
        return failed("persisted Supervisor Host process is not present in the process snapshot")
    if not process_observation_matches(persisted_host, current_host):
        return failed("persisted Supervisor Host process identity changed")

    proxy = process_snapshot.get(proxy_pid)
    if proxy is None:
        return failed("runtime proxy process is not present in the process snapshot")
    if not _observation_identity_complete(proxy):
        return failed("runtime proxy process identity is incomplete", proxy=proxy)
    if expected_proxy is not None and not process_observation_matches(expected_proxy, proxy):
        return failed("runtime proxy process identity changed", proxy=proxy)

    if process_parent_matches(proxy, current_host):
        if expected_mode not in {None, ProxyLaunchMode.DIRECT}:
            return failed(
                "runtime proxy launch mode changed from the persisted provenance",
                proxy=proxy,
            )
        if expected_launcher is not None:
            return failed(
                "direct runtime proxy provenance unexpectedly contains a launcher process",
                proxy=proxy,
            )
        return ProxyLaunchProvenance(
            verified=True,
            mode=ProxyLaunchMode.DIRECT,
            proxy_process=proxy,
        )

    if expected_mode == ProxyLaunchMode.DIRECT:
        return failed("runtime proxy is not a direct child of the Supervisor Host", proxy=proxy)

    spec, spec_failure = _windows_venv_launch_spec()
    if spec_failure is not None or spec is None:
        return failed(
            spec_failure or "Windows venv launch specification is unavailable",
            proxy=proxy,
        )
    if expected_mode not in {None, ProxyLaunchMode.WINDOWS_VENV_TRAMPOLINE}:
        return failed(
            "runtime proxy launch mode is not a verified Windows venv trampoline",
            proxy=proxy,
        )
    if expected_mode == ProxyLaunchMode.WINDOWS_VENV_TRAMPOLINE and expected_launcher is None:
        return failed("persisted Windows venv launcher process identity is missing", proxy=proxy)

    launcher = process_snapshot.get(proxy.parent_pid or -1)
    if launcher is None:
        return failed(
            "runtime proxy parent is not present as the verified Windows venv launcher",
            proxy=proxy,
        )
    if not _observation_identity_complete(launcher):
        return failed(
            "Windows venv launcher process identity is incomplete",
            proxy=proxy,
            launcher=launcher,
        )
    if expected_launcher is not None and not process_observation_matches(
        expected_launcher,
        launcher,
    ):
        return failed(
            "Windows venv launcher process identity changed",
            proxy=proxy,
            launcher=launcher,
        )
    if not _same_executable(
        launcher.executable,
        spec.launcher_executable,
        windows=True,
    ):
        return failed(
            "runtime proxy parent is not the exact current venv Python launcher",
            proxy=proxy,
            launcher=launcher,
        )
    if not _same_executable(proxy.executable, spec.base_executable, windows=True):
        return failed(
            "runtime proxy executable is not sys._base_executable",
            proxy=proxy,
            launcher=launcher,
        )
    path_failure = _proxy_launch_path_failure(spec, path_guard)
    if path_failure is not None:
        return failed(path_failure, proxy=proxy, launcher=launcher)
    if not process_parent_matches(proxy, launcher, windows=True):
        return failed(
            "runtime proxy parent metadata does not match the venv launcher observation",
            proxy=proxy,
            launcher=launcher,
        )
    if not process_parent_matches(launcher, current_host, windows=True):
        return failed(
            "Windows venv launcher parent identity does not match the Supervisor Host",
            proxy=proxy,
            launcher=launcher,
        )
    return ProxyLaunchProvenance(
        verified=True,
        mode=ProxyLaunchMode.WINDOWS_VENV_TRAMPOLINE,
        proxy_process=proxy,
        launcher_process=launcher,
    )


def runtime_verification_failure(
    metadata: CodexRuntimeMetadata,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> str | None:
    desktop_pids = {item.pid for item in metadata.desktop_processes}
    proxy = metadata.proxy_process
    lcb = metadata.lcb_process
    app_server = metadata.app_server_process
    runtime_directory = Path(metadata.runtime_directory)
    codex_home = Path(metadata.codex_home)
    if metadata.schema_version != RUNTIME_METADATA_VERSION:
        return "Supervisor runtime metadata schema is unsupported"
    if metadata.lcb_runtime_contract != LCB_RUNTIME_CONTRACT:
        return "LCB runtime contract is unsupported"
    if metadata.lcb_hardening_revision != LCB_HARDENING_REVISION:
        return "LCB lifecycle hardening revision is unsupported"
    if metadata.endpoint_category != "stdio":
        return "Supervisor runtime endpoint is not private stdio"
    if not metadata.codex_executable:
        return "Supervisor Codex executable identity is missing"
    if not metadata.supervisor_host_instance_id:
        return "Supervisor Host instance identity is missing"
    if metadata.supervisor_parent_process is None:
        return "Supervisor parent process identity is missing"
    if metadata.supervisor_parent_process.pid != metadata.supervisor_parent_pid:
        return "Supervisor parent PID does not match its process identity"
    if not _observation_identity_complete(metadata.supervisor_parent_process):
        return "Supervisor parent process identity is incomplete"
    if metadata.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
        return "runtime ownership is not SUPERVISOR_MANAGED"
    if proxy is None or lcb is None or app_server is None:
        return "process chain metadata is incomplete"
    if metadata.proxy_launch_mode is None:
        return "runtime proxy launch mode is missing"
    process_chain = [metadata.supervisor_parent_process, proxy, lcb, app_server]
    if metadata.proxy_launcher_process is not None:
        process_chain.append(metadata.proxy_launcher_process)
    if len({process.pid for process in process_chain}) != len(process_chain):
        return "Supervisor runtime process identities are not distinct"
    for label, process in (
        ("proxy", proxy),
        ("LCB", lcb),
        ("Codex app-server", app_server),
    ):
        if not _observation_identity_complete(process):
            return f"{label} process identity is incomplete"
    provenance_snapshot = ProcessSnapshotIndex.from_observations(
        [
            metadata.supervisor_parent_process,
            proxy,
            *(
                [metadata.proxy_launcher_process]
                if metadata.proxy_launcher_process is not None
                else []
            ),
        ]
    )
    provenance = proxy_launch_provenance(
        metadata.supervisor_parent_process,
        proxy.pid,
        provenance_snapshot,
        expected_proxy=proxy,
        expected_mode=metadata.proxy_launch_mode,
        expected_launcher=metadata.proxy_launcher_process,
        path_guard=path_guard,
    )
    if not provenance.verified:
        return provenance.failure_reason or "runtime proxy launch provenance is not verified"
    if not process_parent_matches(lcb, proxy):
        return "LCB parent identity does not match the Supervisor runtime proxy"
    if not process_parent_matches(app_server, lcb):
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
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> str | None:
    reason = runtime_verification_failure(metadata, path_guard=path_guard)
    if reason is not None:
        return reason
    if metadata.supervisor_parent_process is None or metadata.proxy_process is None:
        return "runtime proxy launch provenance metadata is incomplete"
    process_snapshot = ProcessSnapshotIndex.from_observations(processes)
    provenance = proxy_launch_provenance(
        metadata.supervisor_parent_process,
        metadata.proxy_process.pid,
        process_snapshot,
        expected_proxy=metadata.proxy_process,
        expected_mode=metadata.proxy_launch_mode,
        expected_launcher=metadata.proxy_launcher_process,
        path_guard=path_guard,
    )
    if not provenance.verified:
        return provenance.failure_reason or "runtime proxy launch provenance changed"
    for label, expected in (
        ("LCB", metadata.lcb_process),
        ("Codex app-server", metadata.app_server_process),
    ):
        if expected is None:
            return f"{label} process identity is missing"
        current = process_snapshot.get(expected.pid)
        if current is None:
            return f"{label} process is not running"
        if not process_observation_matches(expected, current):
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


def process_parent_matches(
    child: ProcessObservation,
    parent: ProcessObservation,
    *,
    windows: bool | None = None,
) -> bool:
    return bool(
        child.parent_pid == parent.pid
        and child.parent_creation_time == parent.creation_time
        and _same_executable(
            child.parent_executable,
            parent.executable,
            windows=windows,
        )
    )


def process_observation_matches(
    expected: ProcessObservation,
    current: ProcessObservation,
    *,
    windows: bool | None = None,
) -> bool:
    return bool(
        expected.pid == current.pid
        and expected.creation_time == current.creation_time
        and _same_executable(expected.executable, current.executable, windows=windows)
        and expected.command_line_fingerprint == current.command_line_fingerprint
        and expected.parent_pid == current.parent_pid
        and expected.parent_creation_time == current.parent_creation_time
        and _same_executable(
            expected.parent_executable,
            current.parent_executable,
            windows=windows,
        )
        and expected.app_server_stdio == current.app_server_stdio
    )


def _same_executable(
    left: str | None,
    right: str | None,
    *,
    windows: bool | None = None,
) -> bool:
    if not left or not right:
        return False
    use_windows = platform.system() == "Windows" if windows is None else windows
    if use_windows:
        return _windows_path_key(left) == _windows_path_key(right)
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(
        os.path.normpath(right)
    )


def _windows_venv_active() -> bool:
    prefix = str(getattr(sys, "prefix", "") or "")
    base_prefix = str(getattr(sys, "base_prefix", "") or "")
    return bool(
        platform.system() == "Windows"
        and prefix
        and base_prefix
        and _windows_path_key(prefix) != _windows_path_key(base_prefix)
    )


def _windows_venv_launch_spec() -> tuple[_WindowsVenvLaunchSpec | None, str | None]:
    if platform.system() != "Windows":
        return None, "runtime proxy has an intermediate parent on a non-Windows platform"
    if not _windows_venv_active():
        return None, "runtime proxy has an intermediate parent outside a CPython venv"
    prefix = str(sys.prefix)
    launcher = str(getattr(sys, "executable", "") or "")
    base_executable = str(getattr(sys, "_base_executable", "") or "")
    expected_launcher = ntpath.join(prefix, "Scripts", "python.exe")
    if not ntpath.isabs(prefix) or not ntpath.isabs(launcher):
        return None, "current Windows venv launcher path is not absolute"
    if not _same_executable(launcher, expected_launcher, windows=True):
        return None, "sys.executable is not the exact current venv Scripts\\python.exe launcher"
    if not base_executable or not ntpath.isabs(base_executable):
        return None, "sys._base_executable is unavailable or not absolute"
    if _same_executable(launcher, base_executable, windows=True):
        return None, "Windows venv launcher and base Python executable are not distinct"
    return _WindowsVenvLaunchSpec(launcher, base_executable), None


def _proxy_launch_path_failure(
    spec: _WindowsVenvLaunchSpec,
    path_guard: PhysicalPathGuard | None,
) -> str | None:
    if path_guard is None:
        return None
    try:
        path_guard.verify_root(Path(spec.launcher_executable), role="process")
        path_guard.verify_root(Path(spec.base_executable), role="process")
    except PhysicalPathVerificationError as exc:
        return f"Windows venv proxy launch path verification failed: {exc.code}"
    return None


def _windows_path_key(value: str | Path) -> str:
    normalized = str(value).replace("/", "\\")
    if normalized.casefold().startswith("\\\\?\\unc\\"):
        normalized = "\\\\" + normalized[8:]
    elif normalized.casefold().startswith("\\\\?\\"):
        normalized = normalized[4:]
    return ntpath.normcase(ntpath.normpath(normalized))


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


def _atomic_json(
    path: Path,
    payload: Mapping[str, Any],
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> None:
    guard = path_guard or PhysicalPathGuard()
    _atomic_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        path_guard=guard,
        role="runtime",
    )


def _atomic_text(
    path: Path,
    content: str,
    *,
    path_guard: PhysicalPathGuard,
    role: str,
) -> None:
    """Write a managed text file only through a verified temporary path."""

    path_guard.ensure_directory(path.parent, role=role)
    path_guard.before_write(path, role=role)
    descriptor, temporary = path_guard.create_temp_file(
        path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        role=role,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        path_guard.replace(temporary, path, role=role)
    finally:
        path_guard.remove(temporary, role=role)


__all__ = [
    "CodexProcessOwnership",
    "CodexRuntimeIsolationError",
    "CodexRuntimeMetadata",
    "LcbRuntimeIsolationUnsupportedError",
    "ProcessInspector",
    "ProcessObservation",
    "ProcessSnapshotIndex",
    "ProxyLaunchMode",
    "ProxyLaunchProvenance",
    "RuntimeOwnershipError",
    "SupervisorCodexRuntimeManager",
    "process_observation_matches",
    "process_parent_matches",
    "proxy_launch_provenance",
    "runtime_process_chain_failure",
    "runtime_verification_failure",
]
