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
from typing import Any, Callable, Sequence


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


class ProcessManager:
    """Provider-neutral local process lifecycle with bounded recovery."""

    def __init__(
        self,
        runtime_dir: str | Path,
        logs_dir: str | Path,
        *,
        launcher: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.runtime_dir = Path(runtime_dir)
        self.logs_dir = Path(logs_dir)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.runtime_dir / "processes.json"
        self._launcher = launcher or subprocess.Popen
        self._clock = clock or time.monotonic
        self._processes: dict[str, ProcessState] = {}
        self._load()

    def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
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
        log_path = self.logs_dir / f"{_safe_name(spec.name)}.log"
        lock_path = self.runtime_dir / f"{_safe_name(spec.name)}.lock"
        try:
            lock_path.open("x", encoding="utf-8").close()
        except FileExistsError:
            lock_state = self.health(spec.name)
            if lock_state.status in {"STALE", "STOPPED", "CRASHED"}:
                lock_path.unlink(missing_ok=True)
                lock_path.open("x", encoding="utf-8").close()
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
        log_handle = log_path.open("a", encoding="utf-8")
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
            lock_path.unlink(missing_ok=True)
            raise
        finally:
            log_handle.close()
        state = ProcessState(
            spec.name,
            "RUNNING",
            pid=int(process.pid),
            log_path=log_path,
            restart_count=restart_count,
            process_identity=_augment_process_identity(
                _process_identity(int(process.pid)),
                spec.command,
                spec.name,
                runtime_identity=spec.runtime_identity,
                instance_id=spec.instance_id,
            ),
            identity_status="VERIFIED",
            ownership=spec.ownership,
            _process=process,
        )
        if process.poll() is not None:
            state.status = "CRASHED" if process.returncode else "STOPPED"
            state.last_exit = process.returncode
            state.pid = None
            state._process = None
            lock_path.unlink(missing_ok=True)
        elif spec.readiness_probe is not None:
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
                self._lock_path(name).unlink(missing_ok=True)
            return self._record(state)
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        state.status = "STOPPED"
        state.last_exit = process.returncode
        state.pid = None
        state._process = None
        self._lock_path(name).unlink(missing_ok=True)
        return self._record(state)

    def restart(self, spec: ManagedProcessSpec) -> ProcessState:
        self.stop(spec.name, timeout=spec.shutdown_timeout)
        return self.start(spec, restart=True)

    def health(self, name: str) -> ProcessState:
        state = self._processes.get(name) or self._load_state(name)
        if state is None:
            return ProcessState(name, "STOPPED")
        process = state._process
        if process is not None:
            returncode = process.poll()
            if returncode is None:
                state.status = "RUNNING"
                return state
            state.status = "CRASHED" if returncode else "STOPPED"
            state.last_exit = returncode
            state.pid = None
            state._process = None
            return self._record(state)
        if state.pid is None:
            return state
        if state.pid and _pid_exists(state.pid):
            current_identity = _process_identity(state.pid)
            if _same_process_identity(state.process_identity, current_identity):
                state.status = "RUNNING"
                state.identity_status = "VERIFIED"
                state.technical_detail = "recovered persisted managed process"
            else:
                state.status = "UNKNOWN"
                state.identity_status = (
                    "PID_REUSED"
                    if state.process_identity and current_identity
                    else "STALE_IDENTITY"
                )
                state.technical_detail = "persisted PID is alive without a verified managed identity"
        else:
            state.status = "STALE"
            state.identity_status = "STALE_PID"
            state.technical_detail = "persisted PID is no longer running"
        return self._record(state)

    def statuses(self) -> list[ProcessState]:
        names = set(self._processes)
        if self.state_path.exists():
            try:
                names.update(json.loads(self.state_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                pass
        return [self.health(name) for name in sorted(names)]

    def repair_stale(self, name: str) -> ProcessState:
        state = self.health(name)
        if state.status in {"STALE", "CRASHED"}:
            state.status = "STOPPED"
            state.pid = None
            state.technical_detail = "stale runtime state cleared"
            self._lock_path(name).unlink(missing_ok=True)
            return self._record(state)
        if state.status == "UNKNOWN" and state.identity_status == "PID_REUSED":
            state.status = "STOPPED"
            state.pid = None
            state.identity_status = "CLEARED_PID_REUSE"
            state.technical_detail = "stale PID reuse record cleared without touching the live process"
            self._lock_path(name).unlink(missing_ok=True)
            return self._record(state)
        return state

    def _record(self, state: ProcessState) -> ProcessState:
        self._processes[state.name] = state
        payload: dict[str, Any] = {}
        if self.state_path.exists():
            try:
                payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
        payload[state.name] = state.as_dict()
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return state

    def _load(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for name in payload:
            self._load_state(name)

    def _load_state(self, name: str) -> ProcessState | None:
        if name in self._processes:
            return self._processes[name]
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
            item = payload.get(name)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(item, dict):
            return None
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
            log_path=Path(item["log_path"]) if item.get("log_path") else None,
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
                lock_path.unlink(missing_ok=True)
                return state
            try:
                ready = spec.readiness_probe()
            except Exception as exc:
                ready = False
                state.technical_detail = f"readiness probe failed: {type(exc).__name__}"
            if ready:
                return state
            if self._clock() >= deadline:
                self._terminate(process, spec.shutdown_timeout)
                state.status = "UNAVAILABLE"
                state.last_exit = process.returncode
                state.pid = None
                state._process = None
                state.technical_detail = "startup timeout"
                lock_path.unlink(missing_ok=True)
                return state
            time.sleep(min(0.05, max(0.001, deadline - self._clock())))

    @staticmethod
    def _terminate(process: Any, timeout: float) -> None:
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
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


def _process_identity(pid: int) -> dict[str, Any] | None:
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
    return (
        os.path.normcase(expected_executable) == os.path.normcase(current_executable)
        and expected_started_at == current_started_at
        and (
            not isinstance(expected.get("command_fingerprint"), str)
            or (
                isinstance(current.get("command_fingerprint"), str)
                and expected["command_fingerprint"] == current["command_fingerprint"]
            )
        )
        and expected.get("parent_pid") == current.get("parent_pid")
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
    parent_identity = _process_identity(parent_pid) if isinstance(parent_pid, int) else None
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
