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
    RUNTIME_METADATA_VERSION,
    SUPERVISOR_CONTRACT_ENV,
    SUPERVISOR_EPOCH_ENV,
    SUPERVISOR_HOST_INSTANCE_ENV,
    SUPERVISOR_METADATA_ENV,
    SUPERVISOR_RUNTIME_ENV,
    SUPERVISOR_TOKEN_ENV,
    CodexRuntimeMetadata,
    ProcessInspector,
    ProcessObservation,
    ProcessSnapshotIndex,
    process_observation_matches,
    process_parent_matches,
    proxy_launch_provenance,
    runtime_process_chain_failure,
)
from .physical import PhysicalPathGuard, PhysicalPathVerificationError
from .process import CodexProcessOwnership


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def _atomic_metadata(path: Path, metadata: CodexRuntimeMetadata) -> None:
    guard = PhysicalPathGuard()
    guard.write_text(
        path,
        json.dumps(metadata.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        role="runtime",
    )


def _read_metadata(
    path: Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> CodexRuntimeMetadata:
    guard = path_guard or PhysicalPathGuard()
    guard.verify_root(path, role="runtime")
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


def _same_process_identity(
    expected: ProcessObservation,
    current: ProcessObservation,
) -> bool:
    return process_observation_matches(expected, current)


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
    path_guard = PhysicalPathGuard()
    try:
        # Validate the metadata path before reading it.  The proxy must never
        # inspect or mutate a path that resolves through Desktop/package state.
        path_guard.verify_root(metadata_path, role="runtime")
        metadata = _read_metadata(metadata_path, path_guard=path_guard)
    except (OSError, ValueError, PhysicalPathVerificationError):
        return 3
    if (
        metadata.schema_version != RUNTIME_METADATA_VERSION
        or metadata_path != Path(os.environ.get(SUPERVISOR_METADATA_ENV, metadata_path))
        or metadata.lcb_runtime_contract != os.environ.get(SUPERVISOR_CONTRACT_ENV)
        or metadata.instance_id != os.environ.get(SUPERVISOR_RUNTIME_ENV)
        or str(metadata.runtime_epoch) != os.environ.get(SUPERVISOR_EPOCH_ENV)
        or metadata.ownership_token_hash != _token_hash()
        or metadata.supervisor_host_instance_id
        != os.environ.get(SUPERVISOR_HOST_INSTANCE_ENV)
        or metadata.codex_executable != os.environ.get("CODEX_EXE")
    ):
        _fail(
            metadata_path,
            metadata,
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
            "runtime proxy identity/token validation failed",
        )
        return 4

    inspector = ProcessInspector()
    path_guard.verify_root(metadata_path, role="runtime")
    path_guard.verify_root(
        Path(metadata.codex_home),
        role="codex_home",
        require_directory=True,
    )
    if not metadata.codex_executable:
        _fail(
            metadata_path,
            metadata,
            "SUPERVISOR_CODEX_RUNTIME_FAILED",
            "Supervisor Codex executable identity is missing",
        )
        return 5
    path_guard.verify_root(Path(metadata.codex_executable), role="process")
    if metadata.supervisor_parent_process is None:
        _fail(
            metadata_path,
            metadata,
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
            "Supervisor parent process identity is missing",
        )
        return 5
    initial_snapshot = ProcessSnapshotIndex.from_observations(inspector.snapshot())
    launch_provenance = proxy_launch_provenance(
        metadata.supervisor_parent_process,
        os.getpid(),
        initial_snapshot,
        path_guard=path_guard,
    )
    if not launch_provenance.verified or launch_provenance.proxy_process is None:
        _fail(
            metadata_path,
            metadata,
            "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
            launch_provenance.failure_reason
            or "runtime proxy launch provenance could not be verified",
        )
        return 5
    proxy_identity = launch_provenance.proxy_process

    try:
        path_guard.before_spawn(list(command), role="runtime")
        child_environment = dict(os.environ)
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

    ready_metadata = metadata
    expected_lcb_identity: ProcessObservation | None = None
    termination_requested = False
    termination_sent = False
    startup_failure_recorded = False
    ownership_lost = False

    def request_termination(_signum: int, _frame: object) -> None:
        nonlocal termination_requested
        termination_requested = True

    def terminate_child(
        processes: Sequence[ProcessObservation] | None = None,
        *,
        require_full_chain: bool,
    ) -> bool:
        nonlocal termination_sent
        if termination_sent or child.poll() is not None:
            return termination_sent
        current_processes = list(processes) if processes is not None else inspector.snapshot()
        if require_full_chain:
            failure = runtime_process_chain_failure(
                ready_metadata,
                current_processes,
                path_guard=path_guard,
            )
        else:
            process_snapshot = ProcessSnapshotIndex.from_observations(current_processes)
            current_provenance = proxy_launch_provenance(
                metadata.supervisor_parent_process,  # type: ignore[arg-type]
                proxy_identity.pid,
                process_snapshot,
                expected_proxy=proxy_identity,
                expected_mode=launch_provenance.mode,
                expected_launcher=launch_provenance.launcher_process,
                path_guard=path_guard,
            )
            current = process_snapshot.get(child.pid)
            failure = None
            if not current_provenance.verified:
                failure = current_provenance.failure_reason
            elif expected_lcb_identity is None or current is None:
                failure = "LCB process identity is unavailable"
            elif not _same_process_identity(expected_lcb_identity, current):
                failure = "LCB process identity changed"
            elif not process_parent_matches(current, proxy_identity):
                failure = "LCB parent identity no longer matches the runtime proxy"
        if failure is not None:
            _fail(
                metadata_path,
                ready_metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                f"LCB termination refused: {failure}",
            )
            return False
        termination_sent = True
        child.terminate()
        return True

    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), request_termination)

    deadline = time.monotonic() + 15.0
    verified = False
    while time.monotonic() < deadline and child.poll() is None:
        processes = inspector.snapshot()
        process_snapshot = ProcessSnapshotIndex.from_observations(processes)
        current_provenance = proxy_launch_provenance(
            metadata.supervisor_parent_process,
            proxy_identity.pid,
            process_snapshot,
            expected_proxy=proxy_identity,
            expected_mode=launch_provenance.mode,
            expected_launcher=launch_provenance.launcher_process,
            path_guard=path_guard,
        )
        if not current_provenance.verified:
            _fail(
                metadata_path,
                metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                current_provenance.failure_reason
                or "runtime proxy launch provenance changed before READY",
            )
            startup_failure_recorded = True
            ownership_lost = True
            break
        lcb_identity = process_snapshot.get(child.pid)
        if (
            expected_lcb_identity is None
            and lcb_identity is not None
            and process_parent_matches(lcb_identity, proxy_identity)
        ):
            expected_lcb_identity = lcb_identity
        if termination_requested:
            if terminate_child(processes, require_full_chain=False):
                _fail(
                    metadata_path,
                    metadata,
                    "SUPERVISOR_CODEX_RUNTIME_FAILED",
                    "runtime proxy termination was requested before READY",
                )
            startup_failure_recorded = True
            break
        app_server = _owned_app_server(processes, child.pid)
        desktops = _desktop_processes(processes)
        if not termination_requested and lcb_identity is not None and app_server is not None:
            candidate = metadata.model_copy(
                update={
                    "ownership": CodexProcessOwnership.SUPERVISOR_MANAGED,
                    "proxy_launch_mode": current_provenance.mode,
                    "proxy_launcher_process": current_provenance.launcher_process,
                    "proxy_process": current_provenance.proxy_process,
                    "lcb_process": lcb_identity,
                    "app_server_process": app_server,
                    "desktop_processes": desktops,
                    "desktop_runtime_present": bool(desktops),
                }
            )
            reason = runtime_process_chain_failure(
                candidate,
                processes,
                path_guard=path_guard,
            )
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

    if not verified:
        if not startup_failure_recorded:
            _fail(
                metadata_path,
                metadata,
                "SUPERVISOR_CODEX_RUNTIME_FAILED",
                (
                    "LCB child did not expose a verifiable Codex stdio app-server"
                    if child.poll() is None
                    else "LCB child exited before runtime ownership was verified"
                ),
            )
        if child.poll() is None and not ownership_lost and not termination_sent:
            terminate_child(require_full_chain=False)
        if child.poll() is None:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        return 7

    while verified and child.poll() is None:
        processes = inspector.snapshot()
        live_failure = runtime_process_chain_failure(
            ready_metadata,
            processes,
            path_guard=path_guard,
        )
        if live_failure is not None:
            _fail(
                metadata_path,
                ready_metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN",
                live_failure,
            )
            ownership_lost = True
            break
        if termination_requested:
            terminate_child(processes, require_full_chain=True)
            break
        time.sleep(0.5)

    if child.poll() is None:
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _fail(
                metadata_path,
                ready_metadata,
                "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"
                if ownership_lost
                else "CODEX_RUNTIME_RECONCILIATION_REQUIRED",
                "owned LCB process remains live after provenance verification failed"
                if ownership_lost
                else "owned LCB process did not stop within the bounded shutdown timeout",
            )
            return 8
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
