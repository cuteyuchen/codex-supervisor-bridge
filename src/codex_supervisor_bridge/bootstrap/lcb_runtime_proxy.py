from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import Sequence

from .codex_isolation import (
    SUPERVISOR_EPOCH_ENV,
    SUPERVISOR_METADATA_ENV,
    SUPERVISOR_RUNTIME_ENV,
    SUPERVISOR_TOKEN_ENV,
    CodexRuntimeMetadata,
    ProcessInspector,
    ProcessObservation,
    runtime_verification_failure,
)
from .process import CodexProcessOwnership


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _atomic_metadata(path: Path, metadata: CodexRuntimeMetadata) -> None:
    temporary = path.with_suffix(path.suffix + ".proxy.tmp")
    temporary.write_text(
        json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_metadata(path: Path) -> CodexRuntimeMetadata:
    return CodexRuntimeMetadata.model_validate_json(path.read_text(encoding="utf-8"))


def _token_hash() -> str:
    token = os.environ.get(SUPERVISOR_TOKEN_ENV, "")
    return hashlib.sha256(token.encode("utf-8")).hexdigest() if token else ""


def _desktop_processes(processes: Sequence[ProcessObservation]) -> list[ProcessObservation]:
    return [
        item
        for item in processes
        if Path(item.executable).name.casefold() in {"codex", "codex.exe"}
        and Path(item.parent_executable or "").name.casefold() in {"chatgpt", "chatgpt.exe"}
    ]


def _owned_app_server(
    processes: Sequence[ProcessObservation],
    lcb_pid: int,
) -> ProcessObservation | None:
    candidates = [
        item
        for item in processes
        if item.parent_pid == lcb_pid and item.app_server_stdio
    ]
    return candidates[0] if len(candidates) == 1 else None


def _same_parent_identity(
    proxy: ProcessObservation,
    processes: Sequence[ProcessObservation],
) -> bool:
    if proxy.parent_pid is None or proxy.parent_creation_time is None:
        return False
    parent = next((item for item in processes if item.pid == proxy.parent_pid), None)
    return bool(
        parent
        and parent.creation_time == proxy.parent_creation_time
        and parent.executable == proxy.parent_executable
    )


def _fail(
    path: Path,
    metadata: CodexRuntimeMetadata,
    code: str,
    detail: str,
) -> None:
    _atomic_metadata(
        path,
        metadata.model_copy(
            update={
                "status": "DEGRADED",
                "ownership": CodexProcessOwnership.UNKNOWN,
                "isolation_verified": False,
                "failure_code": code,
                "technical_detail": detail,
            }
        ),
    )


def run(metadata_path: Path, command: Sequence[str]) -> int:
    command = list(command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        return 2
    try:
        metadata = _read_metadata(metadata_path)
    except (OSError, ValueError):
        return 3
    if (
        metadata_path != Path(os.environ.get(SUPERVISOR_METADATA_ENV, metadata_path))
        or metadata.instance_id != os.environ.get(SUPERVISOR_RUNTIME_ENV)
        or str(metadata.runtime_epoch) != os.environ.get(SUPERVISOR_EPOCH_ENV)
        or metadata.ownership_token_hash != _token_hash()
    ):
        _fail(
            metadata_path,
            metadata,
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
            "runtime proxy identity/token validation failed",
        )
        return 4

    inspector = ProcessInspector()
    proxy_identity = inspector.identity(os.getpid())
    if proxy_identity is None:
        _fail(
            metadata_path,
            metadata,
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
            "runtime proxy process identity is unavailable",
        )
        return 5

    try:
        child_environment = dict(os.environ)
        child_environment.pop(SUPERVISOR_TOKEN_ENV, None)
        child = subprocess.Popen(
            command,
            stdin=None,
            stdout=None,
            stderr=None,
            env=child_environment,
            close_fds=False,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except OSError as exc:
        _fail(
            metadata_path,
            metadata,
            "SUPERVISOR_CODEX_RUNTIME_FAILED",
            f"LCB launch failed: {type(exc).__name__}",
        )
        return 6

    terminating = False

    def terminate_child(_signum: int, _frame: object) -> None:
        nonlocal terminating
        if terminating or child.poll() is not None:
            return
        terminating = True
        current = inspector.identity(child.pid)
        if current is None or current.parent_pid != os.getpid():
            _fail(
                metadata_path,
                metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                "LCB termination refused after process identity changed",
            )
            return
        child.terminate()

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), terminate_child)

    deadline = time.monotonic() + 15.0
    ready_metadata = metadata
    verified = False
    while time.monotonic() < deadline and child.poll() is None:
        processes = inspector.snapshot()
        lcb_identity = next((item for item in processes if item.pid == child.pid), None)
        app_server = _owned_app_server(processes, child.pid)
        desktops = _desktop_processes(processes)
        if lcb_identity is not None and app_server is not None:
            candidate = metadata.model_copy(
                update={
                    "ownership": CodexProcessOwnership.SUPERVISOR_MANAGED,
                    "proxy_process": proxy_identity,
                    "lcb_process": lcb_identity,
                    "app_server_process": app_server,
                    "desktop_processes": desktops,
                    "desktop_runtime_present": bool(desktops),
                }
            )
            reason = runtime_verification_failure(candidate)
            if reason is None:
                ready_metadata = candidate.model_copy(
                    update={
                        "status": "READY",
                        "isolation_verified": True,
                        "failure_code": None,
                        "technical_detail": "Supervisor-owned stdio process chain verified",
                    }
                )
                _atomic_metadata(metadata_path, ready_metadata)
                verified = True
                break
        time.sleep(0.05)
    else:
        if child.poll() is None:
            _fail(
                metadata_path,
                metadata,
                "SUPERVISOR_CODEX_RUNTIME_FAILED",
                "LCB child did not expose a verifiable Codex stdio app-server",
            )

    while verified and child.poll() is None:
        processes = inspector.snapshot()
        if not _same_parent_identity(proxy_identity, processes):
            _fail(
                metadata_path,
                ready_metadata,
                "CODEX_RUNTIME_RECONCILIATION_REQUIRED",
                "Supervisor parent process identity disappeared or changed",
            )
            terminate_child(0, None)
            break
        current_lcb = next((item for item in processes if item.pid == child.pid), None)
        current_app_server = _owned_app_server(processes, child.pid)
        if (
            current_lcb is None
            or current_lcb.creation_time != ready_metadata.lcb_process.creation_time
            or current_app_server is None
            or current_app_server.creation_time
            != ready_metadata.app_server_process.creation_time
        ):
            _fail(
                metadata_path,
                ready_metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                "owned LCB/app-server process identity changed",
            )
            terminate_child(0, None)
            break
        time.sleep(0.5)

    return_code = child.wait()
    try:
        latest = _read_metadata(metadata_path)
        _atomic_metadata(
            metadata_path,
            latest.model_copy(
                update={
                    "status": "STOPPED" if return_code == 0 else "DEGRADED",
                    "isolation_verified": False,
                    "technical_detail": f"LCB process exited with code {return_code}",
                }
            ),
        )
    except (OSError, ValueError):
        pass
    return int(return_code)


def main() -> None:
    args = _parser().parse_args()
    raise SystemExit(run(args.metadata, args.command))


if __name__ == "__main__":
    main()
