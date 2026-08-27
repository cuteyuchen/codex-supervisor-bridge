from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence


@dataclass(frozen=True)
class ManagedProcessSpec:
    name: str
    command: Sequence[str]
    cwd: Path | None = None
    env: dict[str, str] | None = None
    startup_timeout: float = 15.0
    shutdown_timeout: float = 10.0
    max_restarts: int = 3


@dataclass
class ProcessState:
    name: str
    status: str
    pid: int | None = None
    last_exit: int | None = None
    log_path: Path | None = None
    restart_count: int = 0
    technical_detail: str | None = None
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
        if current.status == "RUNNING":
            self.stop(spec.name, timeout=spec.shutdown_timeout)
        log_path = self.logs_dir / f"{_safe_name(spec.name)}.log"
        lock_path = self.runtime_dir / f"{_safe_name(spec.name)}.lock"
        try:
            lock_path.open("x", encoding="utf-8").close()
        except FileExistsError:
            lock_state = self.health(spec.name)
            if lock_state.status in {"STALE", "UNKNOWN", "STOPPED", "CRASHED"}:
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
            _process=process,
        )
        if process.poll() is not None:
            state.status = "CRASHED" if process.returncode else "STOPPED"
            state.last_exit = process.returncode
            state._process = None
            lock_path.unlink(missing_ok=True)
        return self._record(state)

    def stop(self, name: str, *, timeout: float = 10.0) -> ProcessState:
        state = self.health(name)
        process = state._process
        if process is None or state.status != "RUNNING":
            return self._record(state)
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        state.status = "STOPPED"
        state.last_exit = process.returncode
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
            state._process = None
            return self._record(state)
        if state.pid and _pid_exists(state.pid):
            state.status = "UNKNOWN"
            state.technical_detail = "persisted PID is alive without an attached process handle"
        else:
            state.status = "STALE"
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
        if state.status in {"STALE", "UNKNOWN"}:
            state.status = "STOPPED"
            state.pid = None
            state.technical_detail = "stale runtime state cleared"
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
            pid=item.get("pid") if isinstance(item.get("pid"), int) else None,
            last_exit=item.get("last_exit") if isinstance(item.get("last_exit"), int) else None,
            log_path=Path(item["log_path"]) if item.get("log_path") else None,
            restart_count=int(item.get("restart_count", 0)),
            technical_detail=item.get("technical_detail"),
        )
        self._processes[name] = state
        return state

    def _lock_path(self, name: str) -> Path:
        return self.runtime_dir / f"{_safe_name(name)}.lock"


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


def _safe_name(name: str) -> str:
    return "".join(char if char.isalnum() or char in "-_" else "_" for char in name)
