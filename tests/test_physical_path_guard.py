from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from codex_supervisor_bridge.bootstrap import (
    LCB_PHYSICAL_ROOT_MISMATCH,
    SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
    SUPERVISOR_HOST_PATH_VIRTUALIZED,
    SUPERVISOR_RUNTIME_ROOT_MISMATCH,
    AppDataPaths,
    PhysicalPathEvidence,
    PhysicalPathGuard,
    PhysicalPathVerificationError,
    ProcessObservation,
    StandaloneSupervisorHost,
    SupervisorHostOwnership,
    WindowsPhysicalPathInspector,
)
from codex_supervisor_bridge.bootstrap import paths as paths_module


class _StaticProcessInspector:
    def __init__(self, observation: ProcessObservation, *parents: ProcessObservation) -> None:
        self.observation = observation
        self.parents = parents

    def identity(self, _pid: int) -> ProcessObservation:
        return self.observation

    def snapshot(self) -> list[ProcessObservation]:
        return [self.observation, *self.parents]


class _MappingInspector:
    def __init__(self, evidence: dict[str, PhysicalPathEvidence]) -> None:
        self.evidence = evidence

    def inspect(self, path: str | Path) -> PhysicalPathEvidence:
        return self.evidence[str(Path(path))]


class _ProcessMapInspector:
    def __init__(self, *observations: ProcessObservation) -> None:
        self.observations = {item.pid: item for item in observations}

    def identity(self, pid: int) -> ProcessObservation | None:
        return self.observations.get(pid)

    def snapshot(self) -> list[ProcessObservation]:
        return list(self.observations.values())


def _existing(path: Path, physical: str | None = None, *, directory: bool = True) -> PhysicalPathEvidence:
    physical_path = physical or str(path)
    return PhysicalPathEvidence(
        requested_path=str(path),
        physical_path=physical_path,
        exists=True,
        is_directory=directory,
        nearest_existing_path=str(path),
        nearest_existing_physical_path=physical_path,
    )


def _host_observation(
    *,
    parent_pid: int | None = 1,
    parent_executable: str | None = "powershell.exe",
) -> ProcessObservation:
    return ProcessObservation(
        pid=os.getpid(),
        creation_time="host-created",
        executable="supervisor-host.exe",
        command_line_fingerprint="host-command",
        parent_pid=parent_pid,
        parent_creation_time="parent-created" if parent_pid is not None else None,
        parent_executable=parent_executable if parent_pid is not None else None,
    )


def _process_observation(
    pid: int,
    executable: str,
    *,
    creation_time: str | None = None,
    parent_pid: int | None = 1,
    parent_creation_time: str | None = "root-created",
    parent_executable: str | None = "services.exe",
) -> ProcessObservation:
    return ProcessObservation(
        pid=pid,
        creation_time=creation_time or f"created-{pid}",
        executable=executable,
        command_line_fingerprint=f"command-{pid}",
        parent_pid=parent_pid,
        parent_creation_time=parent_creation_time,
        parent_executable=parent_executable,
    )


def _host_for_process_snapshot(
    tmp_path: Path,
    *observations: ProcessObservation,
    process_inspector=None,
) -> StandaloneSupervisorHost:
    root = tmp_path / "CodexSupervisorBridge"
    (root / "components" / "local-codex-bridge" / "2.1.3").mkdir(parents=True)
    (root / "runtime").mkdir()
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    return StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=process_inspector or _ProcessMapInspector(*observations),
    )


def test_canonical_physical_root_passes_and_extended_prefix_normalizes(tmp_path: Path) -> None:
    root = tmp_path / "CodexSupervisorBridge"
    root.mkdir()
    evidence = _existing(root, physical=f"\\\\?\\{root}")

    verified = PhysicalPathGuard(_MappingInspector({str(root): evidence})).verify_root(
        root,
        role="app_data",
        require_directory=True,
    )

    assert verified.verified is True
    assert verified.physical_path == f"\\\\?\\{root}"


def test_packaged_local_cache_physical_view_fails_closed_even_without_package_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = tmp_path / "CodexSupervisorBridge"
    redirected = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge"
    )
    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.physical.current_package_identity",
        lambda: "NO_PACKAGE_IDENTITY",
    )
    guard = PhysicalPathGuard(
        _MappingInspector({str(requested): _existing(requested, physical=redirected)})
    )

    with pytest.raises(PhysicalPathVerificationError) as error:
        guard.verify_root(requested, role="app_data", require_directory=True)

    assert error.value.code == SUPERVISOR_HOST_PATH_VIRTUALIZED


def test_openai_packaged_view_is_rejected_even_for_legacy_migration(
    tmp_path: Path,
) -> None:
    requested = tmp_path / "legacy"
    redirected = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge\legacy"
    )
    guard = PhysicalPathGuard(
        _MappingInspector({str(requested): _existing(requested, physical=redirected)})
    ).for_legacy_migration()

    with pytest.raises(PhysicalPathVerificationError):
        guard.verify_root(requested, role="legacy", require_directory=True)


def test_legacy_marker_write_uses_the_scoped_python_packaged_guard(
    tmp_path: Path,
) -> None:
    root = tmp_path / "legacy"
    root.mkdir()
    physical_root = (
        r"C:\Users\Windows\AppData\Local\Packages\PythonSoftwareFoundation.Python.3.12_qbz5n2kfra8p0"
        r"\LocalCache\Local\CodexSupervisorBridge\legacy"
    )

    class _PackagedPythonInspector:
        def inspect(self, path: str | Path) -> PhysicalPathEvidence:
            requested = Path(path)
            relative = requested.relative_to(root) if requested != root else Path()
            physical = physical_root
            if str(relative) not in {"", "."}:
                physical += "\\" + str(relative).replace("/", "\\")
            return PhysicalPathEvidence(
                requested_path=str(requested),
                physical_path=physical,
                exists=requested.exists(),
                is_directory=requested.is_dir() if requested.exists() else False,
                nearest_existing_path=str(root),
                nearest_existing_physical_path=physical_root,
            )

    guard = PhysicalPathGuard(_PackagedPythonInspector())
    paths_module._write_legacy_inactive_marker(
        root,
        tmp_path / "canonical",
        tmp_path / "backup",
        path_guard=guard,
    )

    assert (root / paths_module.LEGACY_INACTIVE_MARKER).is_file()


def test_missing_directory_verifies_parent_then_rechecks_created_directory(tmp_path: Path) -> None:
    calls: list[Path] = []
    portable = WindowsPhysicalPathInspector(windows=False)

    class _RecordingInspector:
        def inspect(self, path: str | Path) -> PhysicalPathEvidence:
            requested = Path(path)
            calls.append(requested)
            return portable.inspect(requested)

    target = tmp_path / "runtime" / "codex" / "instance"
    verified = PhysicalPathGuard(_RecordingInspector()).ensure_directory(
        target,
        role="runtime",
    )

    assert target.is_dir()
    assert verified.verified is True
    assert target in calls
    assert tmp_path in calls


def test_post_create_physical_recheck_rejects_a_redirected_directory(tmp_path: Path) -> None:
    portable = WindowsPhysicalPathInspector(windows=False)
    target = tmp_path / "runtime"
    redirected = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge\runtime"
    )

    class _FlipInspector:
        def inspect(self, path: str | Path) -> PhysicalPathEvidence:
            requested = Path(path)
            if requested == target and target.exists():
                return _existing(target, physical=redirected)
            return portable.inspect(requested)

    with pytest.raises(PhysicalPathVerificationError) as error:
        PhysicalPathGuard(_FlipInspector()).ensure_directory(target, role="runtime")

    assert error.value.code == SUPERVISOR_RUNTIME_ROOT_MISMATCH


def test_redirected_target_fails_before_write_or_delete(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("unchanged\n", encoding="utf-8")
    redirected = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge\settings.json"
    )
    guard = PhysicalPathGuard(
        _MappingInspector({str(target): _existing(target, physical=redirected, directory=False)})
    )

    with pytest.raises(PhysicalPathVerificationError):
        guard.before_write(target, role="path")
    with pytest.raises(PhysicalPathVerificationError):
        guard.before_delete(target, role="path")

    assert target.read_text(encoding="utf-8") == "unchanged\n"


def test_before_spawn_verifies_path_arguments_in_the_requested_working_tree(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "runner.exe"
    script = tmp_path / "dist" / "src" / "index.js"
    script.parent.mkdir(parents=True)
    executable.write_bytes(b"runner")
    script.write_text("entrypoint", encoding="utf-8")
    redirected = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge\dist\src\index.js"
    )
    guard = PhysicalPathGuard(
        _MappingInspector(
            {
                str(tmp_path): _existing(tmp_path),
                str(executable): _existing(executable),
                str(script): _existing(script, physical=redirected),
            }
        )
    )

    with pytest.raises(PhysicalPathVerificationError) as error:
        guard.before_spawn(
            [str(executable), "dist/src/index.js"],
            cwd=tmp_path,
            role="lcb",
        )

    assert error.value.code == LCB_PHYSICAL_ROOT_MISMATCH


def test_symlink_or_junction_escape_fails_subpath_containment(tmp_path: Path) -> None:
    root = tmp_path / "components"
    escaped = root / "local-codex-bridge"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    guard = PhysicalPathGuard(
        _MappingInspector(
            {
                str(root): _existing(root),
                str(escaped): _existing(escaped, physical=str(outside)),
            }
        )
    )

    with pytest.raises(PhysicalPathVerificationError):
        guard.verify_subpath(escaped, root, role="components", require_directory=True)


def test_standalone_host_accepts_verified_ancestry_and_canonical_roots(tmp_path: Path) -> None:
    root = tmp_path / "CodexSupervisorBridge"
    for child in (root, root / "components" / "local-codex-bridge" / "2.1.3", root / "runtime"):
        child.mkdir(parents=True, exist_ok=True)
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    process = _host_observation()
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=_StaticProcessInspector(process),
    )

    evidence = host.assert_ready()

    assert evidence.physical_root_verified is True
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED
    assert evidence.physical_app_data_root == str(root)


def test_standalone_host_rejects_desktop_ancestry(tmp_path: Path) -> None:
    root = tmp_path / "CodexSupervisorBridge"
    root.mkdir()
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    process = _host_observation(parent_pid=200, parent_executable="ChatGPT.exe")
    desktop_parent = ProcessObservation(
        pid=200,
        creation_time="parent-created",
        executable="ChatGPT.exe",
    )
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=_StaticProcessInspector(process, desktop_parent),
    )

    evidence = host.preflight()

    assert evidence.physical_root_verified is False
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.DESKTOP_EXTERNAL


def test_standalone_host_does_not_read_identity_before_physical_root_proof(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "CodexSupervisorBridge"
    lcb = root / "components" / "local-codex-bridge" / "2.1.3"
    for child in (root, lcb, root / "runtime"):
        child.mkdir(parents=True, exist_ok=True)
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    redirected_root = (
        r"C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_2p2nqsd0c76g0"
        r"\LocalCache\Local\CodexSupervisorBridge"
    )
    evidence = {
        str(root): _existing(root, physical=redirected_root),
        str(root / "components"): _existing(
            root / "components",
            physical=redirected_root + r"\components",
        ),
        str(root / "runtime"): _existing(
            root / "runtime",
            physical=redirected_root + r"\runtime",
        ),
        str(lcb): _existing(lcb, physical=redirected_root + r"\components\local-codex-bridge\2.1.3"),
    }
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(_MappingInspector(evidence)),
        process_inspector=_StaticProcessInspector(_host_observation()),
    )
    read_called = False

    def fail_if_read() -> None:
        nonlocal read_called
        read_called = True
        raise AssertionError("persisted host identity must not be read from a redirected root")

    monkeypatch.setattr(host.environment_guard, "_read_persisted_identity", fail_if_read)

    result = host.preflight()

    assert result.physical_root_verified is False
    assert read_called is False


def test_standalone_host_bootstraps_a_fresh_root_after_parent_verification(tmp_path: Path) -> None:
    root = tmp_path / "fresh" / "CodexSupervisorBridge"
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    process = _host_observation()
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=_StaticProcessInspector(process),
    )

    evidence = host.prepare_app_data()

    assert evidence.physical_root_verified is True
    assert root.is_dir()
    assert (root / "components").is_dir()
    assert (root / "runtime").is_dir()
    assert (root / "data").is_dir()
    assert (root / "logs").is_dir()
    assert (root / "config").is_dir()
    assert (root / "cache").is_dir()
    persisted = json.loads(host.host_identity_path.read_text(encoding="utf-8"))
    assert persisted["command_line_fingerprint"] == process.command_line_fingerprint
    assert persisted["host_instance_id"].startswith("csb-host-")


def test_standalone_host_classifies_windowsapps_codex_ancestry_as_desktop(tmp_path: Path) -> None:
    root = tmp_path / "CodexSupervisorBridge"
    root.mkdir()
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    process = _host_observation(
        parent_pid=201,
        parent_executable=(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_2.2.1.0_x64__2p2nqsd0c76g0"
            r"\OpenAI.Codex.exe"
        ),
    )
    desktop_parent = ProcessObservation(
        pid=201,
        creation_time="parent-created",
        executable=(
            r"C:\Program Files\WindowsApps\OpenAI.Codex_2.2.1.0_x64__2p2nqsd0c76g0"
            r"\OpenAI.Codex.exe"
        ),
    )
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=_StaticProcessInspector(process, desktop_parent),
    )

    evidence = host.preflight()

    assert evidence.physical_root_verified is False
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.DESKTOP_EXTERNAL


def test_standalone_host_classifies_external_pwsh_venv_launcher_from_one_snapshot(
    tmp_path: Path,
) -> None:
    current = _process_observation(
        os.getpid(),
        r"C:\Users\Windows\AppData\Local\Programs\Python\Python312\python.exe",
        creation_time="host-created",
        parent_pid=200,
        parent_creation_time="venv-created",
        parent_executable=r"E:\project\codex-supervisor-bridge\.venv-p66-312\Scripts\python.exe",
    )
    venv_launcher = _process_observation(
        200,
        r"E:\project\codex-supervisor-bridge\.venv-p66-312\Scripts\python.exe",
        creation_time="venv-created",
        parent_pid=201,
        parent_creation_time="pwsh-created",
        parent_executable="pwsh.exe",
    )
    pwsh = _process_observation(201, "pwsh.exe", creation_time="pwsh-created")

    evidence = _host_for_process_snapshot(tmp_path, current, venv_launcher, pwsh).preflight()

    assert evidence.physical_root_verified is True
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED


def test_standalone_host_classifies_desktop_ancestry_from_same_snapshot(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=210,
        parent_creation_time="venv-created",
        parent_executable="python-venv.exe",
    )
    venv = _process_observation(
        210,
        "python-venv.exe",
        creation_time="venv-created",
        parent_pid=211,
        parent_creation_time="chatgpt-created",
        parent_executable="ChatGPT.exe",
    )
    chatgpt = _process_observation(211, "ChatGPT.exe", creation_time="chatgpt-created")

    evidence = _host_for_process_snapshot(tmp_path, current, venv, chatgpt).preflight()

    assert evidence.physical_root_verified is False
    assert evidence.failure_code == SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.DESKTOP_EXTERNAL


def test_standalone_host_classifies_missing_ancestor_as_unknown(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=220,
        parent_creation_time="missing-created",
        parent_executable="python-venv.exe",
    )

    evidence = _host_for_process_snapshot(tmp_path, current).preflight()

    assert evidence.physical_root_verified is False
    assert evidence.failure_code == SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.UNKNOWN


def test_standalone_host_uses_one_snapshot_for_ancestry_verdict(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=230,
        parent_creation_time="venv-created",
        parent_executable="python-venv.exe",
    )
    venv = _process_observation(
        230,
        "python-venv.exe",
        creation_time="venv-created",
        parent_pid=231,
        parent_creation_time="pwsh-created",
        parent_executable="pwsh.exe",
    )
    pwsh = _process_observation(231, "pwsh.exe", creation_time="pwsh-created")

    class _FlappingInspector:
        def __init__(self) -> None:
            self.snapshot_call_count = 0

        def snapshot(self) -> list[ProcessObservation]:
            self.snapshot_call_count += 1
            if self.snapshot_call_count == 1:
                return [current, venv, pwsh]
            return [current, venv]

        def identity(self, _pid: int) -> ProcessObservation:
            raise AssertionError("Host verdict must not perform a second live identity read")

    inspector = _FlappingInspector()
    evidence = _host_for_process_snapshot(
        tmp_path,
        process_inspector=inspector,
    ).preflight()

    assert inspector.snapshot_call_count == 1
    assert evidence.physical_root_verified is True
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED


def test_standalone_host_rejects_parent_creation_time_reuse(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=240,
        parent_creation_time="original-created",
        parent_executable="python-venv.exe",
    )
    reused_parent = _process_observation(
        240,
        "python-venv.exe",
        creation_time="reused-created",
    )

    evidence = _host_for_process_snapshot(tmp_path, current, reused_parent).preflight()

    assert evidence.physical_root_verified is False
    assert evidence.failure_code == SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.UNKNOWN


def test_standalone_host_detects_deep_desktop_ancestry(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=250,
        parent_creation_time="venv-created",
        parent_executable="python-venv.exe",
    )
    venv = _process_observation(
        250,
        "python-venv.exe",
        creation_time="venv-created",
        parent_pid=251,
        parent_creation_time="pwsh-created",
        parent_executable="pwsh.exe",
    )
    pwsh = _process_observation(
        251,
        "pwsh.exe",
        creation_time="pwsh-created",
        parent_pid=252,
        parent_creation_time="chatgpt-created",
        parent_executable="ChatGPT.exe",
    )
    chatgpt = _process_observation(252, "ChatGPT.exe", creation_time="chatgpt-created")

    evidence = _host_for_process_snapshot(tmp_path, current, venv, pwsh, chatgpt).preflight()

    assert evidence.physical_root_verified is False
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.DESKTOP_EXTERNAL


def test_standalone_host_does_not_use_package_identity_to_override_desktop_ancestry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.host.current_package_identity",
        lambda: "NO_PACKAGE_IDENTITY",
    )
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=260,
        parent_creation_time="chatgpt-created",
        parent_executable="ChatGPT.exe",
    )
    chatgpt = _process_observation(260, "ChatGPT.exe", creation_time="chatgpt-created")

    evidence = _host_for_process_snapshot(tmp_path, current, chatgpt).preflight()

    assert evidence.identity is not None
    assert evidence.identity.package_identity == "NO_PACKAGE_IDENTITY"
    assert evidence.identity.ownership == SupervisorHostOwnership.DESKTOP_EXTERNAL
    assert evidence.failure_code == SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE


def test_standalone_host_keeps_path_safe_but_ancestry_unknown_fail_closed(tmp_path: Path) -> None:
    current = _process_observation(
        os.getpid(),
        "python.exe",
        creation_time="host-created",
        parent_pid=270,
        parent_creation_time="missing-created",
        parent_executable="python-venv.exe",
    )

    evidence = _host_for_process_snapshot(tmp_path, current).preflight()

    assert evidence.app_data.verified is True
    assert evidence.components.verified is True
    assert evidence.runtime.verified is True
    assert evidence.lcb.verified is True
    assert evidence.physical_root_verified is False
    assert evidence.failure_code == SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE
    assert evidence.identity is not None
    assert evidence.identity.ownership == SupervisorHostOwnership.UNKNOWN


def _write_persisted_host_authority(
    path: Path,
    observation: ProcessObservation,
    *,
    instance_id: str = "csb-host-authority",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "host_instance_id": instance_id,
                "host_ownership": SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED.value,
                "pid": observation.pid,
                "creation_time": observation.creation_time,
                "executable": observation.executable,
                "command_line_fingerprint": observation.command_line_fingerprint,
                "parent_pid": observation.parent_pid,
                "parent_creation_time": observation.parent_creation_time,
                "parent_executable": observation.parent_executable,
            }
        ),
        encoding="utf-8",
    )


def _inherited_host_fixture(tmp_path: Path, *, parent_matches: bool = True):
    root = tmp_path / "CodexSupervisorBridge"
    lcb = root / "components" / "local-codex-bridge" / "2.1.3"
    runtime = root / "runtime"
    for child in (lcb, runtime):
        child.mkdir(parents=True, exist_ok=True)
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(root)},
        system="Windows",
    )
    authority = ProcessObservation(
        pid=os.getpid() + 100000,
        creation_time="authority-created",
        executable="standalone-supervisor-host.exe",
        command_line_fingerprint="authority-command",
        parent_pid=1,
        parent_creation_time="system-created",
        parent_executable="services.exe",
    )
    current = ProcessObservation(
        pid=os.getpid(),
        creation_time="child-created",
        executable="supervisor-service.exe",
        command_line_fingerprint="child-command",
        parent_pid=authority.pid if parent_matches else authority.pid + 1,
        parent_creation_time=authority.creation_time,
        parent_executable=authority.executable,
    )
    identity_path = runtime / "host-identity.json"
    _write_persisted_host_authority(identity_path, authority)
    environment = {
        "CODEX_SUPERVISOR_DATA_DIR": str(root),
        "CODEX_SUPERVISOR_HOST_INSTANCE_ID": "csb-host-authority",
        "CODEX_SUPERVISOR_HOST_IDENTITY_PATH": str(identity_path),
    }
    host = StandaloneSupervisorHost(
        paths=paths,
        physical_guard=PhysicalPathGuard(WindowsPhysicalPathInspector(windows=False)),
        process_inspector=_ProcessMapInspector(current, authority),
        environ=environment,
    )
    return host, identity_path


def test_inherited_supervisor_child_reuses_verified_host_authority(tmp_path: Path) -> None:
    host, identity_path = _inherited_host_fixture(tmp_path)
    before = identity_path.read_bytes()

    evidence = host.assert_ready()
    identity = host.ensure_identity(evidence=evidence)

    assert identity.host_instance_id == "csb-host-authority"
    assert evidence.host_identity_persisted is True
    assert identity_path.read_bytes() == before


def test_inherited_supervisor_child_rejects_wrong_parent_authority(tmp_path: Path) -> None:
    host, _ = _inherited_host_fixture(tmp_path, parent_matches=False)

    evidence = host.preflight()

    assert evidence.physical_root_verified is False
    assert evidence.failure_code == "SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE"


def test_inherited_supervisor_child_never_replaces_authority_record(tmp_path: Path) -> None:
    host, identity_path = _inherited_host_fixture(tmp_path)
    before = identity_path.read_bytes()

    host.prepare_app_data()

    assert identity_path.read_bytes() == before
