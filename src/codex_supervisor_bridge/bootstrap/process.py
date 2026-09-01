from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .physical import PhysicalPathGuard, PhysicalPathVerificationError


class CodexProcessOwnership(str, Enum):
    """Fail-closed ownership classification for Codex-related processes."""

    DESKTOP_EXTERNAL = "DESKTOP_EXTERNAL"
    SUPERVISOR_MANAGED = "SUPERVISOR_MANAGED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ManagedProcessSpec:
    name: str
    command: Sequence[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None
    startup_timeout: float = 15.0
    shutdown_timeout: float = 10.0
    max_restarts: int = 3
    readiness_probe: Callable[[], bool] | None = None
    ownership: CodexProcessOwnership = CodexProcessOwnership.SUPERVISOR_MANAGED
    runtime_identity: str | None = None
    instance_id: str | None = None


@dataclass
class ProcessState:
    name: str
    status: str
    pid: int | None = None
    last_exit: int | None = None
    log_path: Path | None = None
    restart_count: int = 0
    technical_detail: str | None = None
    process_identity: dict[str, Any] | None = None
    identity_status: str | None = None
    ownership: CodexProcessOwnership = CodexProcessOwnership.UNKNOWN
    _process: Any = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "pid": self.pid,
            "last_exit": self.last_exit,
            "log_path": str(self.log_path) if self.log_path else None,
            "restart_count": self.restart_count,
            "technical_detail": self.technical_detail,
            "process_identity": self.process_identity,
            "identity_status": self.identity_status,
            "ownership": self.ownership.value,
        }


@dataclass(frozen=True)
class PersistedProcessClassification:
    """Read-only classification of a persisted process record."""

    status: str
    identity_status: str | None
    live: bool
    identity_verified: bool
    ownership_verified: bool
    pid_reused: bool


class _ProcessLifecycleRefused(RuntimeError):
    """Raised when a destructive process action cannot be proven safe."""


class ProcessManager:
    """Provider-neutral local process lifecycle with bounded recovery."""

    def __init__(
        self,
        runtime_dir: str | Path,
        logs_dir: str | Path,
        *,
        launcher: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
        path_guard: PhysicalPathGuard | None = None,
        identity_reader: Callable[[int], dict[str, Any] | None] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.logs_dir = Path(logs_dir)
        self.path_guard = path_guard or PhysicalPathGuard()
        self.state_path = self.runtime_dir / "processes.json"
        self._launcher = launcher or subprocess.Popen
        self._clock = clock or time.monotonic
        self._identity_reader = identity_reader
        self._processes: dict[str, ProcessState] = {}
        self._ensure_storage()
        self._load()

    def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
        if spec.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
            return self._record(
                ProcessState(
                    spec.name,
                    "UNKNOWN",
                    ownership=spec.ownership,
                    technical_detail=(
                        "process start refused because only SUPERVISOR_MANAGED "
                        "resources may be lifecycle-managed"
                    ),
                )
            )
        current = self.health(spec.name)
        if current.status == "RUNNING" and not restart:
            return current
        restart_count = current.restart_count + (1 if restart or current.status == "STALE" else 0)
        if restart_count > spec.max_restarts:
            return self._record(
                ProcessState(
                    spec.name,
                    "UNAVAILABLE",
                    restart_count=restart_count,
                    last_exit=current.last_exit,
                    log_path=current.log_path,
                    technical_detail="restart limit reached",
                )
            )
        if current.status == "UNKNOWN" and current.identity_status == "PID_REUSED":
            current = self.repair_stale(spec.name)
        if current.status == "UNKNOWN":
            return self._record(
                ProcessState(
                    spec.name,
                    "UNKNOWN",
                    pid=current.pid,
                    last_exit=current.last_exit,
                    log_path=current.log_path,
                    restart_count=current.restart_count,
                    technical_detail=(
                        "persisted PID is alive without a verified managed identity"
                    ),
                    process_identity=current.process_identity,
                )
            )
        if current.status == "RUNNING":
            stopped = self.stop(spec.name, timeout=spec.shutdown_timeout)
            if stopped.status != "STOPPED":
                return stopped
        self._ensure_storage()
        self.path_guard.before_spawn(
            list(spec.command),
            cwd=spec.cwd,
            role="runtime",
        )
        log_path = self.logs_dir / f"{_safe_name(spec.name)}.log"
        lock_path = self.runtime_dir / f"{_safe_name(spec.name)}.lock"
        self.path_guard.before_write(log_path, role="runtime")
        self.path_guard.before_write(lock_path, role="runtime")
        try:
            lock_path.open("x", encoding="utf-8").close()
            self.path_guard.verify_root(lock_path, role="runtime")
        except FileExistsError:
            lock_state = self.health(spec.name)
            if lock_state.status in {"STALE", "STOPPED", "CRASHED"}:
                self._safe_unlink(lock_path)
                lock_path.open("x", encoding="utf-8").close()
                self.path_guard.verify_root(lock_path, role="runtime")
            else:
                return ProcessState(
                    spec.name,
                    "UNAVAILABLE",
                    pid=lock_state.pid,
                    last_exit=lock_state.last_exit,
                    log_path=lock_state.log_path,
                    restart_count=lock_state.restart_count,
                    technical_detail="component lock is held",
                )
        try:
            log_handle = log_path.open("a", encoding="utf-8")
            self.path_guard.verify_root(log_path, role="runtime")
        except Exception:
            self._safe_unlink(lock_path)
            raise
        try:
            process = self._launcher(
                list(spec.command),
                cwd=str(spec.cwd) if spec.cwd else None,
                env=spec.env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
            )
        except Exception:
            log_handle.close()
            self._safe_unlink(lock_path)
            raise
        finally:
            log_handle.close()
        process_identity = _augment_process_identity(
            self._read_process_identity(int(process.pid)),
            spec.command,
            spec.name,
            runtime_identity=spec.runtime_identity,
            instance_id=spec.instance_id,
        )
        state = ProcessState(
            spec.name,
            "RUNNING" if _complete_process_identity(process_identity) else "UNKNOWN",
            pid=int(process.pid),
            log_path=log_path,
            restart_count=restart_count,
            process_identity=process_identity,
            identity_status=(
                "VERIFIED" if _complete_process_identity(process_identity) else "UNKNOWN"
            ),
            ownership=(
                spec.ownership
                if _complete_process_identity(process_identity)
                else CodexProcessOwnership.UNKNOWN
            ),
            technical_detail=(
                None
                if _complete_process_identity(process_identity)
                else "managed process identity is incomplete; lifecycle is fail-closed"
            ),
            _process=process,
        )
        if process.poll() is not None:
            state.status = "CRASHED" if process.returncode else "STOPPED"
            state.last_exit = process.returncode
            state.pid = None
            state._process = None
            self._safe_unlink(lock_path)
        elif state.status == "RUNNING" and spec.readiness_probe is not None:
            state = self._wait_for_readiness(state, spec, lock_path)
        return self._record(state)

    def stop(self, name: str, *, timeout: float = 10.0) -> ProcessState:
        state = self.health(name)
        process = state._process
        if state.status == "RUNNING" and state.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
            state.status = "UNKNOWN"
            state.technical_detail = (
                "destructive lifecycle refused because process ownership is not "
                "SUPERVISOR_MANAGED"
            )
            return self._record(state)
        if state.status == "RUNNING" and process is None:
            state.status = "UNKNOWN"
            state.technical_detail = (
                "destructive lifecycle refused because the verified persisted process "
                "has no owned process handle"
            )
            return self._record(state)
        if process is None or state.status != "RUNNING":
            if state.status in {"CRASHED", "STALE"}:
                self._safe_unlink(self._lock_path(name))
            return self._record(state)
        try:
            self._assert_destructive_target(state, process)
            self.path_guard.verify_root(self.runtime_dir, role="runtime", require_directory=True)
            self._terminate(
                process,
                timeout,
                verify=lambda: self._assert_destructive_target(state, process),
            )
        except (_ProcessLifecycleRefused, PhysicalPathVerificationError) as exc:
            state.status = "UNKNOWN"
            state.identity_status = "OWNERSHIP_UNVERIFIED"
            state.technical_detail = str(exc)
            return self._record(state)
        state.status = "STOPPED"
        state.last_exit = process.returncode
        state.pid = None
        state._process = None
        self._safe_unlink(self._lock_path(name))
        return self._record(state)

    def restart(self, spec: ManagedProcessSpec) -> ProcessState:
        current = self.health(spec.name)
        if current.status == "UNKNOWN" and current.identity_status == "PID_REUSED":
            # A reused PID is stale state, not an owned process. Clear only the
            # persisted record before considering a fresh Supervisor launch.
            cleared = self.repair_stale(spec.name)
            if cleared.status != "STOPPED":
                return cleared
            return self.start(spec, restart=True)
        stopped = self.stop(spec.name, timeout=spec.shutdown_timeout)
        if stopped.status in {"CRASHED", "STALE"}:
            stopped = self.repair_stale(spec.name)
        if stopped.status != "STOPPED":
            return stopped
        return self.start(spec, restart=True)

    def health(self, name: str) -> ProcessState:
        state = self._processes.get(name) or self._load_state(name)
        if state is None:
            return ProcessState(name, "STOPPED")
        process = state._process
        if process is not None:
            returncode = process.poll()
            if returncode is None:
                if (
                    state.ownership == CodexProcessOwnership.UNKNOWN
                    or (
                        state.ownership == CodexProcessOwnership.SUPERVISOR_MANAGED
                        and state.identity_status != "VERIFIED"
                    )
                ):
                    state.status = "UNKNOWN"
                else:
                    state.status = "RUNNING"
                return state
            state.status = "CRASHED" if returncode else "STOPPED"
            state.last_exit = returncode
            state.pid = None
            state._process = None
            return self._record(state)
        if state.pid is None:
            return state
        classification = classify_persisted_process(
            status=state.status,
            pid=state.pid,
            process_identity=state.process_identity,
            ownership=state.ownership,
            pid_exists=_pid_exists,
            identity_reader=self._read_process_identity,
        )
        state.status = classification.status
        state.identity_status = classification.identity_status
        if classification.status == "RUNNING":
            state.technical_detail = "recovered persisted managed process"
        elif classification.status == "STALE":
            state.technical_detail = "persisted PID is no longer running"
        elif classification.status == "UNKNOWN":
            state.technical_detail = (
                "persisted PID is alive without a verified managed identity"
            )
        return self._record(state)

    def statuses(self) -> list[ProcessState]:
        names = set(self._processes)
        if self.state_path.exists():
            try:
                self.path_guard.verify_root(self.state_path, role="runtime")
                names.update(json.loads(self.state_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, PhysicalPathVerificationError):
                pass
        return [self.health(name) for name in sorted(names)]

    def repair_stale(self, name: str) -> ProcessState:
        state = self.health(name)
        if state.status in {"STALE", "CRASHED"}:
            state.status = "STOPPED"
            state.pid = None
            state.technical_detail = "stale runtime state cleared"
            self._safe_unlink(self._lock_path(name))
            return self._record(state)
        if state.status == "UNKNOWN" and state.identity_status == "PID_REUSED":
            state.status = "STOPPED"
            state.pid = None
            state.identity_status = "CLEARED_PID_REUSE"
            state.technical_detail = "stale PID reuse record cleared without touching the live process"
            self._safe_unlink(self._lock_path(name))
            return self._record(state)
        return state

    def _record(self, state: ProcessState) -> ProcessState:
        self._ensure_storage()
        self.path_guard.before_write(self.state_path, role="runtime")
        self._processes[state.name] = state
        payload: dict[str, Any] = {}
        if self.state_path.exists():
            try:
                self.path_guard.verify_root(self.state_path, role="runtime")
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, PhysicalPathVerificationError):
                payload = {}
        payload[state.name] = state.as_dict()
        descriptor, temporary = self.path_guard.create_temp_file(
            self.state_path.parent,
            prefix="processes-",
            suffix=".tmp",
            role="runtime",
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.path_guard.replace(temporary, self.state_path, role="runtime")
        finally:
            self.path_guard.remove(temporary, role="runtime")
        return state

    def _ensure_storage(self) -> None:
        self.path_guard.ensure_directory(self.runtime_dir, role="runtime")
        self.path_guard.ensure_directory(self.logs_dir, role="path")

    def _safe_unlink(self, path: Path) -> None:
        if not path.exists():
            return
        self.path_guard.remove(path, role="runtime")

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            self.path_guard.verify_root(self.state_path, role="runtime")
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, PhysicalPathVerificationError):
            return
        for name in payload:
            self._load_state(name)

    def _load_state(self, name: str) -> ProcessState | None:
        if name in self._processes:
            return self._processes[name]
        try:
            self.path_guard.verify_root(self.state_path, role="runtime")
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            item = payload.get(name)
        except (OSError, json.JSONDecodeError, PhysicalPathVerificationError):
            return None
        if not isinstance(item, dict):
            return None
        log_path: Path | None = None
        if item.get("log_path"):
            candidate_log = Path(str(item["log_path"]))
            try:
                self.path_guard.verify_subpath(
                    candidate_log,
                    self.logs_dir,
                    role="runtime",
                )
            except PhysicalPathVerificationError:
                candidate_log = None
            log_path = candidate_log
        state = ProcessState(
            name=name,
            status=str(item.get("status", "STOPPED")),
            pid=(
                item.get("pid")
                if isinstance(item.get("pid"), int)
                and str(item.get("status", "STOPPED")) not in {"CRASHED", "STOPPED"}
                else None
            ),
            last_exit=item.get("last_exit") if isinstance(item.get("last_exit"), int) else None,
            log_path=log_path,
            restart_count=int(item.get("restart_count", 0)),
            technical_detail=item.get("technical_detail"),
            process_identity=(
                item.get("process_identity")
                if isinstance(item.get("process_identity"), dict)
                else None
            ),
            identity_status=(
                item.get("identity_status")
                if isinstance(item.get("identity_status"), str)
                else None
            ),
            ownership=(
                CodexProcessOwnership(item["ownership"])
                if item.get("ownership") in {member.value for member in CodexProcessOwnership}
                else CodexProcessOwnership.UNKNOWN
            ),
        )
        self._processes[name] = state
        return state

    def _lock_path(self, name: str) -> Path:
        return self.runtime_dir / f"{_safe_name(name)}.lock"

    def _wait_for_readiness(
        self,
        state: ProcessState,
        spec: ManagedProcessSpec,
        lock_path: Path,
    ) -> ProcessState:
        process = state._process
        if process is None or spec.readiness_probe is None:
            return state
        deadline = self._clock() + max(0.0, spec.startup_timeout)
        while True:
            returncode = process.poll()
            if returncode is not None:
                state.status = "CRASHED" if returncode else "STOPPED"
                state.last_exit = returncode
                state.pid = None
                state._process = None
                self._safe_unlink(lock_path)
                return state
            try:
                ready = spec.readiness_probe()
            except Exception as exc:
                ready = False
                state.technical_detail = f"readiness probe failed: {type(exc).__name__}"
            if ready:
                return state
            if self._clock() >= deadline:
                try:
                    self._assert_destructive_target(state, process)
                    self.path_guard.verify_root(
                        self.runtime_dir,
                        role="runtime",
                        require_directory=True,
                    )
                    self._terminate(
                        process,
                        spec.shutdown_timeout,
                        verify=lambda: self._assert_destructive_target(state, process),
                    )
                except (_ProcessLifecycleRefused, PhysicalPathVerificationError) as exc:
                    state.status = "UNKNOWN"
                    state.identity_status = "OWNERSHIP_UNVERIFIED"
                    state.technical_detail = f"startup timeout; {exc}"
                    return state
                state.status = "UNAVAILABLE"
                state.last_exit = process.returncode
                state.pid = None
                state._process = None
                state.technical_detail = "startup timeout"
                self._safe_unlink(lock_path)
                return state
            time.sleep(min(0.05, max(0.001, deadline - self._clock())))

    def _read_process_identity(self, pid: int) -> dict[str, Any] | None:
        if self._identity_reader is not None:
            return self._identity_reader(pid)
        return _process_identity(pid)

    def _assert_destructive_target(self, state: ProcessState, process: Any) -> None:
        if state.ownership != CodexProcessOwnership.SUPERVISOR_MANAGED:
            raise _ProcessLifecycleRefused(
                "destructive lifecycle refused because process ownership is not "
                "SUPERVISOR_MANAGED"
            )
        if state.pid is None or state.process_identity is None:
            raise _ProcessLifecycleRefused(
                "destructive lifecycle refused because process identity is unavailable"
            )
        if process.poll() is not None:
            raise _ProcessLifecycleRefused(
                "destructive lifecycle skipped because the process already exited"
            )
        current = self._read_process_identity(state.pid)
        if not _complete_process_identity(current) or not _same_process_identity(
            state.process_identity,
            current,
        ):
            raise _ProcessLifecycleRefused(
                "destructive lifecycle refused because process identity changed or is unknown"
            )

    @staticmethod
    def _terminate(
        process: Any,
        timeout: float,
        *,
        verify: Callable[[], None] | None = None,
    ) -> None:
        if process.poll() is not None:
            return
        if verify is not None:
            verify()
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if process.poll() is None:
                if verify is not None:
                    verify()
                process.kill()
                process.wait(timeout=2)


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_process_identity(pid) is not None
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False
    return True


def classify_persisted_process(
    *,
    status: str,
    pid: int | None,
    process_identity: Mapping[str, Any] | None,
    ownership: CodexProcessOwnership | str | None,
    pid_exists: Callable[[int], bool] | None = None,
    identity_reader: Callable[[int], dict[str, Any] | None] | None = None,
) -> PersistedProcessClassification:
    """Classify persisted state without mutating it or its target process.

    A live PID is not enough to prove a managed process survived.  The record
    is considered live only when the complete process identity and explicit
    Supervisor ownership both match.  A complete identity mismatch is a
    reusable stale record and can be cleared without touching the live PID;
    every other live-but-unproven case remains UNKNOWN.
    """

    normalized_status = str(status).upper()
    if normalized_status not in {"RUNNING", "UNKNOWN"}:
        return PersistedProcessClassification(
            status=normalized_status,
            identity_status=None,
            live=False,
            identity_verified=False,
            ownership_verified=False,
            pid_reused=False,
        )
    if not isinstance(pid, int) or pid <= 0:
        return PersistedProcessClassification(
            status="UNKNOWN",
            identity_status="MISSING_PID",
            live=False,
            identity_verified=False,
            ownership_verified=False,
            pid_reused=False,
        )

    probe = pid_exists or _pid_exists
    try:
        live = bool(probe(pid))
    except Exception:
        # A failed liveness probe cannot authorize stale cleanup.
        return PersistedProcessClassification(
            status="UNKNOWN",
            identity_status="PID_PROBE_FAILED",
            live=True,
            identity_verified=False,
            ownership_verified=False,
            pid_reused=False,
        )
    if not live:
        return PersistedProcessClassification(
            status="STALE",
            identity_status="STALE_PID",
            live=False,
            identity_verified=False,
            ownership_verified=False,
            pid_reused=False,
        )

    reader = identity_reader or _process_identity
    try:
        current_identity = reader(pid)
    except Exception:
        current_identity = None
    expected_identity = dict(process_identity) if isinstance(process_identity, Mapping) else None
    expected_complete = _complete_process_identity(expected_identity)
    current_complete = _complete_process_identity(current_identity)
    if expected_complete and current_complete:
        if _same_process_identity(expected_identity, current_identity):
            ownership_value = (
                ownership.value
                if isinstance(ownership, CodexProcessOwnership)
                else str(ownership or "")
            )
            ownership_verified = ownership_value == CodexProcessOwnership.SUPERVISOR_MANAGED.value
            return PersistedProcessClassification(
                status="RUNNING" if ownership_verified else "UNKNOWN",
                identity_status="VERIFIED" if ownership_verified else "OWNERSHIP_UNKNOWN",
                live=True,
                identity_verified=True,
                ownership_verified=ownership_verified,
                pid_reused=False,
            )
        return PersistedProcessClassification(
            status="UNKNOWN",
            identity_status="PID_REUSED",
            live=True,
            identity_verified=False,
            ownership_verified=False,
            pid_reused=True,
        )

    # Creation time and executable are still useful evidence when an older
    # record lacks the newer parent/command fields.  A mismatch is safe stale
    # state; equality is deliberately still UNKNOWN because identity proof is
    # incomplete.
    if _minimal_identity(expected_identity) and _minimal_identity(current_identity):
        if (
            expected_identity.get("started_at") != current_identity.get("started_at")
            or os.path.normcase(str(expected_identity.get("executable")))
            != os.path.normcase(str(current_identity.get("executable")))
        ):
            return PersistedProcessClassification(
                status="UNKNOWN",
                identity_status="PID_REUSED",
                live=True,
                identity_verified=False,
                ownership_verified=False,
                pid_reused=True,
            )
    return PersistedProcessClassification(
        status="UNKNOWN",
        identity_status="STALE_IDENTITY",
        live=True,
        identity_verified=False,
        ownership_verified=False,
        pid_reused=False,
    )


def _process_identity(pid: int) -> dict[str, Any] | None:
    identity = _process_identity_base(pid)
    if identity is None:
        return None
    parent_pid = identity.get("parent_pid")
    identity["parent_process_identity"] = (
        _process_identity_base(parent_pid) if isinstance(parent_pid, int) else None
    )
    return identity


def _process_identity_base(pid: int) -> dict[str, Any] | None:
    if pid <= 0:
        return None
    if sys.platform == "win32":
        return _windows_process_identity(pid)
    proc_root = Path("/proc") / str(pid)
    try:
        executable = str((proc_root / "exe").resolve(strict=True))
        fields = (proc_root / "stat").read_text(encoding="utf-8").split()
        started_at = int(fields[21])
    except (OSError, ValueError, IndexError):
        return None
    try:
        parent_pid = int(fields[3])
        command_line = (proc_root / "cmdline").read_bytes().replace(b"\x00", b"\x1f")
    except (OSError, ValueError, IndexError):
        parent_pid = None
        command_line = b""
    return {
        "executable": executable,
        "started_at": started_at,
        "parent_pid": parent_pid,
        "command_fingerprint": hashlib.sha256(command_line).hexdigest()
        if command_line
        else None,
    }


def _windows_process_identity(pid: int) -> dict[str, Any] | None:
    import ctypes
    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return None
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return None
        started_at = (created.dwHighDateTime << 32) | created.dwLowDateTime
        supplemental = _windows_process_supplement(pid)
        return {
            "executable": buffer.value,
            "started_at": started_at,
            "parent_pid": supplemental.get("parent_pid"),
            "command_fingerprint": supplemental.get("command_fingerprint"),
        }
    finally:
        kernel32.CloseHandle(handle)


def _windows_process_supplement(pid: int) -> dict[str, Any]:
    """Read parent/command identity without retaining or logging command text."""

    script = (
        "$p=Get-CimInstance Win32_Process -Filter \"ProcessId="
        + str(pid)
        + "\" -ErrorAction SilentlyContinue;"
        "if($null -ne $p){[pscustomobject]@{ParentProcessId=$p.ParentProcessId;"
        "CommandLine=$p.CommandLine}|ConvertTo-Json -Compress}"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        payload = json.loads(completed.stdout) if completed.returncode == 0 and completed.stdout else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return {}
    command_line = payload.get("CommandLine")
    return {
        "parent_pid": (
            int(payload["ParentProcessId"])
            if isinstance(payload.get("ParentProcessId"), int)
            else None
        ),
        "command_fingerprint": (
            hashlib.sha256(command_line.encode("utf-8")).hexdigest()
            if isinstance(command_line, str) and command_line
            else None
        ),
    }


def _same_process_identity(
    expected: dict[str, Any] | None,
    current: dict[str, Any] | None,
) -> bool:
    if expected is None or current is None:
        return False
    expected_executable = expected.get("executable")
    current_executable = current.get("executable")
    expected_started_at = expected.get("started_at")
    current_started_at = current.get("started_at")
    if not isinstance(expected_executable, str) or not isinstance(current_executable, str):
        return False
    if not isinstance(expected_started_at, int) or not isinstance(current_started_at, int):
        return False
    expected_command = expected.get("command_fingerprint")
    current_command = current.get("command_fingerprint")
    if not isinstance(expected_command, str) or not isinstance(current_command, str):
        return False
    if expected_command != current_command or expected.get("parent_pid") != current.get("parent_pid"):
        return False
    expected_parent = expected.get("parent_process_identity")
    current_parent = current.get("parent_process_identity")
    if not isinstance(expected_parent, dict) or not isinstance(current_parent, dict):
        return False
    return (
        os.path.normcase(expected_executable) == os.path.normcase(current_executable)
        and expected_started_at == current_started_at
        and _same_identity_record(expected_parent, current_parent)
    )


def _complete_process_identity(identity: dict[str, Any] | None) -> bool:
    if identity is None:
        return False
    parent = identity.get("parent_process_identity")
    return bool(
        isinstance(identity.get("executable"), str)
        and isinstance(identity.get("started_at"), int)
        and isinstance(identity.get("command_fingerprint"), str)
        and identity.get("parent_pid") is not None
        and isinstance(parent, dict)
        and isinstance(parent.get("executable"), str)
        and isinstance(parent.get("started_at"), int)
        and isinstance(parent.get("command_fingerprint"), str)
    )


def _minimal_identity(identity: Mapping[str, Any] | None) -> bool:
    return bool(
        isinstance(identity, Mapping)
        and isinstance(identity.get("executable"), str)
        and bool(identity.get("executable"))
        and isinstance(identity.get("started_at"), int)
    )


def _same_identity_record(expected: dict[str, Any], current: dict[str, Any]) -> bool:
    expected_executable = expected.get("executable")
    current_executable = current.get("executable")
    return bool(
        isinstance(expected_executable, str)
        and isinstance(current_executable, str)
        and os.path.normcase(expected_executable) == os.path.normcase(current_executable)
        and expected.get("started_at") == current.get("started_at")
        and expected.get("parent_pid") == current.get("parent_pid")
        and expected.get("command_fingerprint") == current.get("command_fingerprint")
    )


def _augment_process_identity(
    identity: dict[str, Any] | None,
    command: Sequence[str],
    managed_name: str,
    *,
    runtime_identity: str | None = None,
    instance_id: str | None = None,
) -> dict[str, Any] | None:
    if identity is None:
        return None
    parent_pid = identity.get("parent_pid")
    parent_identity = identity.get("parent_process_identity")
    if not isinstance(parent_identity, dict) and isinstance(parent_pid, int):
        parent_identity = _process_identity_base(parent_pid)
    return {
        **identity,
        "launch_command_fingerprint": _command_fingerprint(command),
        "managed_instance": managed_name,
        "parent_process_identity": parent_identity,
        "runtime_identity": runtime_identity,
        "instance_id": instance_id,
    }


def _command_fingerprint(command: Sequence[str]) -> str:
    payload = "\x00".join(str(item) for item in command)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
