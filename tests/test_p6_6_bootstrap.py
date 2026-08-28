from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from mcp.client.auth.oauth2 import OAuthClientInformationFull, OAuthClientProvider
from mcp.shared.auth import OAuthToken
from pydantic import ValidationError

from codex_supervisor_bridge.bootstrap import (
    AppConfig,
    AppDataPaths,
    AuthorizationStatus,
    CodexReadinessDetector,
    CommandAuthorizationPolicy,
    CommandRequest,
    CommandSession,
    CommandSessionStatus,
    CommandVerdict,
    ComponentHealth,
    ConfigStore,
    DevSpaceBootstrap,
    DevSpaceLocalOAuthDriver,
    DevSpaceVersionCompatibility,
    Doctor,
    DoctorOptions,
    DoctorStatus,
    FirstAuthorizationFlow,
    HarnessStep,
    HarnessTrace,
    HealthStatus,
    LocalCodexBridgeBootstrap,
    LocalCodexBridgeBootstrapConfig,
    ManagedProcessSpec,
    MemorySecretStore,
    PortAllocator,
    ProcessManager,
    ProcessState,
    ProfileABHarness,
    ProfileScenarioRunner,
    ScenarioObservation,
    SecretTokenStorage,
    SecureRemoteAccessConfig,
    SecureRemoteAccessController,
    SecureRemoteAccessValidator,
    authorize_command,
    redact_oauth_payload,
)
from codex_supervisor_bridge.bootstrap.service import _find_executable
from codex_supervisor_bridge.mcp.server import _is_loopback_host, build_parser
from codex_supervisor_bridge.mcp.server import main as cli_main
from codex_supervisor_bridge.memory.models import ActiveWriter


class FakeDoctor:
    def __init__(
        self,
        *,
        status: HealthStatus = HealthStatus.READY,
        components: list[ComponentHealth] | None = None,
    ) -> None:
        self.status = status
        self.components = components or [
            ComponentHealth(
                capability="Local workspace",
                status=HealthStatus.READY,
                user_message="Local workspace is ready.",
            )
        ]

    def run(self, options: object | None = None) -> DoctorStatus:
        del options
        return DoctorStatus(status=self.status, components=list(self.components))


def test_windows_paths_follow_local_app_data(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        home=tmp_path,
        environ={"LOCALAPPDATA": r"C:\Users\Test\AppData\Local"},
        system="Windows",
    )
    assert str(paths.root).replace("/", "\\").endswith(r"AppData\Local\CodexSupervisorBridge")
    assert paths.database == paths.data / "supervisor.db"
    assert paths.settings == paths.config / "settings.json"


def test_config_migrates_old_shape_and_invalid_config_degrades(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    paths.config.mkdir(parents=True)
    paths.settings.write_text(
        json.dumps({"version": 0, "default_development_style": "web_first"}),
        encoding="utf-8",
    )
    store = ConfigStore(paths=paths)
    loaded = store.load()
    assert loaded.migrated is True
    assert loaded.config.basic.development_style.value == "web_first"
    assert json.loads(paths.settings.read_text(encoding="utf-8"))["config_version"] == 1

    paths.settings.write_text("{not-json", encoding="utf-8")
    degraded = store.load()
    assert degraded.status == "DEGRADED"
    assert degraded.config == AppConfig.safe_defaults(paths)

    paths.settings.write_text(
        json.dumps({"config_version": 1, "advanced": {"oauth_detail": {"access_token": "secret"}}}),
        encoding="utf-8",
    )
    secret_config = store.load()
    assert secret_config.status == "DEGRADED"


def test_configure_persists_user_intent_and_cli_flags(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService
    from codex_supervisor_bridge.bootstrap.configuration import CommandPolicy, DevelopmentStyle

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    project = tmp_path / "project"
    project.mkdir()
    local_repository = tmp_path / "Local-Codex-Bridge"
    local_repository.mkdir()
    service = BootstrapService(paths=paths)
    result = service.configure(
        project_directory=project,
        development_style=DevelopmentStyle.CODEX_FIRST,
        local_command_policy=CommandPolicy.ALLOW,
        allow_chatgpt_codex_delegation=True,
        automatic_git_commit=True,
        automatic_pull_request=False,
        local_codex_repository=local_repository,
        node_executable="C:/Program Files/nodejs/node.exe",
    )

    config = ConfigStore(paths=paths).load().config
    assert config.basic.project_directory == project.resolve()
    assert config.basic.development_style == DevelopmentStyle.CODEX_FIRST
    assert config.basic.local_command_policy == CommandPolicy.ALLOW
    assert config.basic.allow_chatgpt_codex_delegation is True
    assert config.basic.automatic_git_commit is True
    assert config.basic.automatic_pull_request is False
    assert config.advanced.local_codex_repository == local_repository.resolve()
    assert config.advanced.executable_paths["node"] == "C:/Program Files/nodejs/node.exe"
    assert result.project_directory == str(project.resolve())

    namespace = build_parser().parse_args(
        [
            "configure",
            "--project",
            str(project),
            "--style",
            "web_first",
            "--allow-codex-delegation",
            "--no-auto-commit",
            "--local-codex-repository",
            str(local_repository),
            "--node",
            "C:/Program Files/nodejs/node.exe",
        ]
    )
    assert namespace.command == "configure"
    assert namespace.style == "web_first"
    assert namespace.allow_codex_delegation is True
    assert namespace.auto_commit is False
    assert namespace.local_codex_repository == local_repository
    assert namespace.node == "C:/Program Files/nodejs/node.exe"


def test_port_allocator_prefers_configured_port_then_recovers_conflict() -> None:
    allocator = PortAllocator(start=39000, end=39005)
    first = allocator.reserve(39000)
    try:
        second = allocator.reserve(39000)
        try:
            assert first.port == 39000
            assert second.port != first.port
        finally:
            second.release()
    finally:
        first.release()


def test_port_allocator_can_exclude_persisted_ports() -> None:
    allocator = PortAllocator(start=39010, end=39012)
    lease = allocator.reserve(39010, excluded={39010})
    try:
        assert lease.port != 39010
    finally:
        lease.release()


class DummyProcess:
    _next_pid = 50000

    def __init__(self) -> None:
        self.pid = DummyProcess._next_pid
        DummyProcess._next_pid += 1
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0


def test_process_manager_tracks_lifecycle_and_stale_pid(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    spec = ManagedProcessSpec(name="bridge", command=["dummy"])
    running = manager.start(spec)
    assert running.status == "RUNNING"
    assert manager.health("bridge").status == "RUNNING"
    stopped = manager.stop("bridge")
    assert stopped.status == "STOPPED"
    assert stopped.last_exit == 0

    state_path = tmp_path / "runtime" / "processes.json"
    state_path.write_text(
        json.dumps({"old": {"status": "RUNNING", "pid": 999999, "restart_count": 0}}),
        encoding="utf-8",
    )
    recovered = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    assert recovered.health("old").status == "STALE"


def test_process_manager_honors_startup_readiness_timeout(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    state = manager.start(
        ManagedProcessSpec(
            name="slow",
            command=["dummy"],
            startup_timeout=0.01,
            readiness_probe=lambda: False,
        )
    )

    assert state.status == "UNAVAILABLE"
    assert state.technical_detail == "startup timeout"
    assert launched[0].returncode == 0


def test_process_manager_does_not_clear_ambiguous_live_pid(tmp_path: Path, monkeypatch) -> None:
    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    (tmp_path / "runtime" / "processes.json").write_text(
        json.dumps({"bridge": {"status": "RUNNING", "pid": 42, "restart_count": 0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("codex_supervisor_bridge.bootstrap.process._pid_exists", lambda pid: pid == 42)
    assert manager.health("bridge").status == "UNKNOWN"
    assert manager.repair_stale("bridge").status == "UNKNOWN"


def test_process_manager_does_not_start_duplicate_for_ambiguous_live_pid(tmp_path: Path, monkeypatch) -> None:
    launched: list[object] = []

    manager = ProcessManager(
        tmp_path / "runtime",
        tmp_path / "logs",
        launcher=lambda *args, **kwargs: launched.append((args, kwargs)),
    )
    (tmp_path / "runtime" / "processes.json").write_text(
        json.dumps({"bridge": {"status": "RUNNING", "pid": 42, "restart_count": 0}}),
        encoding="utf-8",
    )
    monkeypatch.setattr("codex_supervisor_bridge.bootstrap.process._pid_exists", lambda pid: pid == 42)

    state = manager.start(ManagedProcessSpec(name="bridge", command=["dummy"]), restart=True)

    assert state.status == "UNKNOWN"
    assert launched == []


def test_bootstrap_resolves_configured_absolute_executable_path(tmp_path: Path) -> None:
    executable = tmp_path / "devspace.exe"
    executable.write_text("placeholder", encoding="utf-8")

    assert _find_executable(str(executable)) == str(executable)


def test_devspace_current_config_filename(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    bootstrap = DevSpaceBootstrap.from_app_data(paths, port=39101)
    stale = bootstrap.config_directory / "config.jsonc"
    stale.parent.mkdir(parents=True)
    stale.write_text("{ stale prototype }", encoding="utf-8")
    config_path = bootstrap.write_config()

    assert bootstrap.config_directory == paths.config / "devspace"
    assert config_path == paths.config / "devspace" / "config.json"
    assert config_path.exists()
    assert not stale.exists()


def test_devspace_current_flat_config_matches_upstream_contract(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    project = tmp_path / "project"
    project.mkdir()
    bootstrap = DevSpaceBootstrap.from_app_data(paths, port=39101, project_directory=project)
    document = json.loads(bootstrap.write_config().read_text(encoding="utf-8"))

    assert document == {
        "host": "127.0.0.1",
        "port": 39101,
        "allowedRoots": [str(project.resolve())],
        "publicBaseUrl": None,
        "allowedHosts": ["localhost", "127.0.0.1"],
        "stateDir": str(paths.data / "devspace"),
        "worktreeRoot": str(paths.cache / "devspace" / "worktrees"),
        "artifactsEnabled": False,
        "agentDir": "~/.codex",
        "subagents": False,
    }
    assert bootstrap.config.upstream_compatibility() == {
        "tested_versions": ["1.0.5", "1.0.8"],
        "supported_version_range": ">=1.0.5,<1.1",
        "configuration_kind": "v1_0_flat",
        "configuration_file": "config.json",
    }


def test_bridge_config_reads_through_upstream_flat_parser(tmp_path: Path) -> None:
    fixture = json.loads(
        (Path(__file__).parent / "fixtures" / "devspace" / "upstream-v1.0.8.json").read_text(
            encoding="utf-8"
        )
    )
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    project = tmp_path / "project"
    project.mkdir()
    bootstrap = DevSpaceBootstrap.from_app_data(paths, port=39101, project_directory=project)
    secrets = MemorySecretStore()
    bootstrap.write_config()
    bootstrap.prepare_auth(secrets)

    def load_devspace_files(directory: Path) -> dict[str, object]:
        return {
            "dir": str(directory),
            "config": json.loads((directory / "config.json").read_text(encoding="utf-8")),
            "auth": json.loads((directory / "auth.json").read_text(encoding="utf-8")),
        }

    files = load_devspace_files(bootstrap.config_directory)
    config = files["config"]
    auth = files["auth"]
    assert isinstance(config, dict)
    assert isinstance(auth, dict)
    assert set(config).issubset(set(fixture["accepted_configuration_fields"]))
    assert config["host"] == "127.0.0.1"
    assert config["port"] == 39101
    assert config["allowedRoots"] == [str(project.resolve())]
    assert config["publicBaseUrl"] is None
    assert auth["ownerToken"] == secrets.get("devspace-owner-token")


def test_upstream_release_contract_fixture_remains_current() -> None:
    fixture_path = Path(__file__).parent / "fixtures" / "devspace" / "upstream-v1.0.8.json"
    contract = json.loads(fixture_path.read_text(encoding="utf-8"))

    assert contract["package"] == "@waishnav/devspace"
    assert contract["version"] == "1.0.8"
    assert contract["node_requirement"] == ">=22.19 <27"
    assert contract["configuration_file"] == "config.json"
    assert contract["authentication_file"] == "auth.json"
    assert contract["configuration_kind"] == "flat"
    assert contract["public_base_url_is_nullable"] is True
    assert contract["owner_token_minimum_length"] == 16
    assert contract["required_tools"] == [
        "apply_patch",
        "exec_command",
        "open_workspace",
        "read",
        "show_changes",
        "write_stdin",
    ]


def test_local_codex_bridge_upstream_release_contract_fixture_remains_current() -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "local-codex-bridge"
        / "upstream-v2.1.3.json"
    )
    contract = json.loads(fixture_path.read_text(encoding="utf-8"))
    bootstrap = LocalCodexBridgeBootstrap(
        LocalCodexBridgeBootstrapConfig(launch_command=["node", "bridge.js"])
    )

    assert contract["repository"] == "zoeynine/Local-Codex-Bridge"
    assert contract["version"] == "2.1.3"
    assert contract["node_requirement"] == ">=24"
    assert contract["entrypoint"] == "dist/src/index.js"
    assert contract["npm_start_is_development_only"] is True
    assert sorted(contract["required_tools"]) == bootstrap.config.required_tools


def test_devspace_version_compatibility_matches_tested_releases() -> None:
    assert DevSpaceVersionCompatibility.parse_version("devspace 1.0.8") == (1, 0, 8)
    assert DevSpaceVersionCompatibility.is_supported("1.0.5") is True
    assert DevSpaceVersionCompatibility.is_supported("1.0.8") is True
    assert DevSpaceVersionCompatibility.is_supported("1.1.0") is False
    assert DevSpaceVersionCompatibility.is_supported("unknown") is False


def test_devspace_managed_config_directory_is_used_by_repair_and_process(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    project = tmp_path / "project"
    project.mkdir()
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    secrets = MemorySecretStore()
    RepairService(paths=paths, secret_store=secrets).repair(project_directory=project)
    config = ConfigStore(paths=paths).load().config
    bootstrap = DevSpaceBootstrap.from_app_data(
        paths,
        port=config.advanced.ports["devspace"],
        project_directory=project,
    )
    spec = bootstrap.process_spec()

    assert bootstrap.config_directory == paths.config / "devspace"
    assert bootstrap.auth_path == paths.config / "devspace" / "auth.json"
    assert spec.cwd == bootstrap.config_directory
    assert spec.env is not None
    assert spec.env["DEVSPACE_CONFIG_DIR"] == str(bootstrap.config_directory)
    assert str(Path.home() / ".devspace") not in spec.env.values()
    assert "DEVSPACE_OAUTH_OWNER_TOKEN" not in spec.env
    auth = json.loads(bootstrap.auth_path.read_text(encoding="utf-8"))
    owner_token = secrets.get("devspace-owner-token")
    assert auth["ownerToken"] == owner_token
    assert len(owner_token) >= 32
    assert bootstrap.prepare_auth(secrets) == bootstrap.auth_path
    assert secrets.get("devspace-owner-token") == owner_token


def test_devspace_is_not_chatgpt_facing_public_endpoint(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    RepairService(paths=paths, secret_store=MemorySecretStore()).repair(project_directory=tmp_path)
    generated = json.loads(paths.generated_mcp_config.read_text(encoding="utf-8"))
    workspace_document = json.loads(
        (paths.config / "devspace" / "config.json").read_text(encoding="utf-8")
    )

    assert list(generated["mcpServers"]) == ["codex-supervisor-bridge"]
    assert generated["mcpServers"]["codex-supervisor-bridge"]["url"].startswith("http://127.0.0.1:")
    assert workspace_document["publicBaseUrl"] is None


def test_bootstrap_start_reports_supervisor_launch_failure(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class FailingProcessManager:
        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del spec, restart
            raise OSError("launcher unavailable")

    service = BootstrapService(paths=AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    ), process_manager=FailingProcessManager())
    result = service.start(project_directory=tmp_path)

    assert result.repairs[-1].action == "start_supervisor"
    assert result.repairs[-1].status == HealthStatus.UNAVAILABLE


def test_bootstrap_start_uses_local_codex_protocol_bootstrap(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50000)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.process_commands["local_codex_bridge"] = "node bridge.js"
    config_store.save(config)
    process_manager = RecordingProcessManager()

    result = BootstrapService(
        paths=paths,
        config_store=config_store,
        process_manager=process_manager,
        doctor=FakeDoctor(
            components=[
                ComponentHealth(
                    capability="Local workspace",
                    status=HealthStatus.READY,
                    user_message="Local workspace is ready.",
                ),
                ComponentHealth(
                    capability="Codex control",
                    status=HealthStatus.READY,
                    user_message="Codex control is ready.",
                ),
            ]
        ),
    ).start(project_directory=tmp_path)

    assert not any(spec.name == "local_codex_bridge" for spec in process_manager.started)
    action = next(
        item
        for item in result.repairs
        if item.action == "agent_session:local_codex_bridge"
    )
    assert action.status == HealthStatus.READY
    assert action.advanced["launch_command"] == ["node", "bridge.js"]
    assert action.advanced["daemon"] is False


def test_bootstrap_start_uses_local_codex_repository_launch(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50001)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    repository = tmp_path / "Local-Codex-Bridge"
    entrypoint = repository / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.local_codex_repository = repository
    config.advanced.executable_paths["node"] = "C:/Program Files/nodejs/node.exe"
    config_store.save(config)
    process_manager = RecordingProcessManager()

    result = BootstrapService(
        paths=paths,
        config_store=config_store,
        process_manager=process_manager,
        doctor=FakeDoctor(
            components=[
                ComponentHealth(
                    capability="Local workspace",
                    status=HealthStatus.READY,
                    user_message="Local workspace is ready.",
                ),
                ComponentHealth(
                    capability="Codex control",
                    status=HealthStatus.READY,
                    user_message="Codex control is ready.",
                ),
            ]
        ),
    ).start(project_directory=tmp_path)

    assert not any(spec.name == "local_codex_bridge" for spec in process_manager.started)
    action = next(
        item
        for item in result.repairs
        if item.action == "agent_session:local_codex_bridge"
    )
    assert action.status == HealthStatus.READY
    assert action.advanced["launch_command"] == [
        "C:/Program Files/nodejs/node.exe",
        str(entrypoint.resolve()),
    ]
    assert action.advanced["managed_by"] == "agent_session_manager"


def test_bootstrap_start_skips_incompatible_devspace_release(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50010)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    process_manager = RecordingProcessManager()
    doctor = FakeDoctor(
        status=HealthStatus.DEGRADED,
        components=[
            ComponentHealth(
                capability="Local workspace",
                status=HealthStatus.DEGRADED,
                repairable=False,
                user_message="Local workspace needs a compatible release.",
                recommended_action="repair_local_workspace",
                advanced={
                    "version": "1.1.0",
                    "upstream_compatibility": {"compatible": False},
                },
            )
        ],
    )

    result = BootstrapService(
        paths=paths,
        process_manager=process_manager,
        doctor=doctor,
    ).start(project_directory=tmp_path)

    devspace_action = next(item for item in result.repairs if item.action == "start_process:devspace")
    assert devspace_action.status == HealthStatus.DEGRADED
    assert devspace_action.requires_user_action is True
    assert not any(spec.name == "devspace" for spec in process_manager.started)
    assert any(spec.name == "supervisor" for spec in process_manager.started)


def test_bootstrap_start_skips_local_codex_bridge_with_old_node(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50011)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    repository = tmp_path / "Local-Codex-Bridge"
    entrypoint = repository / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.local_codex_repository = repository
    config.advanced.executable_paths["node"] = "C:/Program Files/nodejs/node.exe"
    config_store.save(config)
    process_manager = RecordingProcessManager()
    doctor = FakeDoctor(
        status=HealthStatus.DEGRADED,
        components=[
            ComponentHealth(
                capability="Local workspace",
                status=HealthStatus.READY,
                user_message="Local workspace is ready.",
            ),
            ComponentHealth(
                capability="Codex control",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message="Codex control needs Node.js 24 or newer.",
                recommended_action="repair_codex_control",
                advanced={"node_version": "v20.19.0"},
            ),
        ],
    )

    result = BootstrapService(
        paths=paths,
        config_store=config_store,
        process_manager=process_manager,
        doctor=doctor,
    ).start(project_directory=tmp_path)

    control_action = next(
        item for item in result.repairs if item.action == "agent_session:local_codex_bridge"
    )
    assert control_action.status == HealthStatus.DEGRADED
    assert control_action.requires_user_action is True
    assert not any(spec.name == "local_codex_bridge" for spec in process_manager.started)


def test_bootstrap_start_launches_all_healthy_components(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50012)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    devspace_executable = tmp_path / "devspace.cmd"
    devspace_executable.write_text("placeholder", encoding="utf-8")
    repository = tmp_path / "Local-Codex-Bridge"
    entrypoint = repository / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.executable_paths["devspace"] = str(devspace_executable)
    config.advanced.executable_paths["node"] = "C:/Program Files/nodejs/node.exe"
    config.advanced.local_codex_repository = repository
    config_store.save(config)
    process_manager = RecordingProcessManager()
    doctor = FakeDoctor(
        components=[
            ComponentHealth(
                capability="Local workspace",
                status=HealthStatus.READY,
                user_message="Local workspace is ready.",
            ),
            ComponentHealth(
                capability="Codex control",
                status=HealthStatus.READY,
                user_message="Codex control is ready.",
            ),
        ]
    )

    result = BootstrapService(
        paths=paths,
        config_store=config_store,
        process_manager=process_manager,
        doctor=doctor,
    ).start(project_directory=tmp_path)

    assert [spec.name for spec in process_manager.started] == ["devspace", "supervisor"]
    devspace_spec = next(spec for spec in process_manager.started if spec.name == "devspace")
    assert list(devspace_spec.command) == [str(devspace_executable), "serve"]
    assert devspace_spec.env is not None
    assert devspace_spec.env["DEVSPACE_CONFIG_DIR"] == str(paths.config / "devspace")
    assert any(item.action == "start_process:devspace" for item in result.repairs)
    assert any(item.action == "agent_session:local_codex_bridge" for item in result.repairs)
    assert not any(spec.name == "local_codex_bridge" for spec in process_manager.started)


def test_bootstrap_start_uses_managed_node_to_launch_devspace(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap import BootstrapService

    class RecordingProcessManager:
        def __init__(self) -> None:
            self.started: list[ManagedProcessSpec] = []

        def statuses(self) -> list[ProcessState]:
            return []

        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

        def start(self, spec: ManagedProcessSpec, *, restart: bool = False) -> ProcessState:
            del restart
            self.started.append(spec)
            return ProcessState(spec.name, "RUNNING", pid=50013)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    node = tmp_path / "node.exe"
    node.write_text("placeholder", encoding="utf-8")
    entrypoint = tmp_path / "devspace-cli.js"
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.executable_paths["devspace"] = str(node)
    config.advanced.executable_paths["devspace_entrypoint"] = str(entrypoint)
    config_store.save(config)
    process_manager = RecordingProcessManager()

    result = BootstrapService(
        paths=paths,
        config_store=config_store,
        process_manager=process_manager,
        doctor=FakeDoctor(
            components=[
                ComponentHealth(
                    capability="Local workspace",
                    status=HealthStatus.READY,
                    user_message="Local workspace is ready.",
                ),
            ]
        ),
    ).start(project_directory=tmp_path)

    devspace_spec = next(spec for spec in process_manager.started if spec.name == "devspace")
    assert list(devspace_spec.command) == [str(node), str(entrypoint), "serve"]
    assert [spec.name for spec in process_manager.started] == ["devspace", "supervisor"]
    assert result.repairs


def test_http_mcp_bind_must_be_loopback() -> None:
    assert _is_loopback_host("127.0.0.1") is True
    assert _is_loopback_host("::1") is True
    assert _is_loopback_host("localhost") is True
    assert _is_loopback_host("0.0.0.0") is False


def test_doctor_user_view_does_not_leak_provider_details(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    doctor = Doctor(paths=paths)
    status = doctor.run()
    rendered = str(status.user_summary).lower()
    assert "devspace" not in rendered
    assert "codex-control-plane" not in rendered
    assert any(item["capability"] == "Codex" for item in status.user_summary)
    assert status.status == HealthStatus.DEGRADED


def test_repair_creates_data_and_mcp_config_without_secrets(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    service = RepairService(paths=paths)
    actions = service.repair(project_directory=tmp_path)
    assert paths.database.parent.exists()
    assert paths.generated_mcp_config.exists()
    assert any(action.action == "generate_mcp_config" for action in actions)
    assert "token" not in paths.generated_mcp_config.read_text(encoding="utf-8").lower()


def test_repair_recovers_invalid_config_to_safe_defaults(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    paths.config.mkdir(parents=True)
    paths.settings.write_text("{not-json", encoding="utf-8")
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    doctor = Doctor(paths=paths)
    before = doctor.run()
    config_component = before.component("Configuration")
    assert config_component is not None
    assert config_component.status == HealthStatus.DEGRADED
    assert config_component.repairable is True

    RepairService(paths=paths, secret_store=MemorySecretStore()).repair(
        before,
        project_directory=tmp_path,
    )

    after = ConfigStore(paths=paths).load()
    assert after.status == "READY"
    assert after.config.config_version == 1


def test_repair_persists_distinct_supervisor_and_workspace_ports(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    service = RepairService(paths=paths, port_allocator=PortAllocator(start=39200, end=39202))
    service.repair(project_directory=tmp_path)
    ports = ConfigStore(paths=paths).load().config.advanced.ports

    assert ports["supervisor"] != ports["devspace"]


def test_repair_recovers_stale_process_and_keeps_unknown_for_user(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap.repair import RepairService

    class FakeProcessManager:
        def __init__(self) -> None:
            self.states = [
                ProcessState("devspace", "STALE", pid=999999),
                ProcessState("supervisor", "UNKNOWN", pid=42),
            ]

        def statuses(self) -> list[ProcessState]:
            return list(self.states)

        def repair_stale(self, name: str) -> ProcessState:
            state = next(item for item in self.states if item.name == name)
            state.status = "STOPPED"
            state.pid = None
            state.technical_detail = "stale runtime state cleared"
            return state

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    actions = RepairService(
        paths=paths,
        process_manager=FakeProcessManager(),
        secret_store=MemorySecretStore(),
    ).repair(project_directory=tmp_path)

    stale_action = next(item for item in actions if item.action == "repair_process:devspace")
    assert stale_action.status == HealthStatus.READY
    assert stale_action.requires_user_action is False
    unknown_action = next(item for item in actions if item.action == "repair_process:supervisor")
    assert unknown_action.status == HealthStatus.DEGRADED
    assert unknown_action.requires_user_action is True


def test_doctor_marks_crashed_supervisor_repairable(tmp_path: Path) -> None:
    class CrashedProcessManager:
        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "CRASHED", last_exit=1)

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    status = Doctor(
        paths=paths,
        command_runner=runner,
        process_manager=CrashedProcessManager(),
    ).run(DoctorOptions(check_optional_components=False))
    supervisor = status.component("Supervisor Bridge")

    assert supervisor is not None
    assert supervisor.status == HealthStatus.DEGRADED
    assert supervisor.repairable is True
    assert supervisor.recommended_action == "restart_supervisor"
    assert supervisor.advanced["status"] == "CRASHED"


def test_doctor_marks_stopped_supervisor_needing_start(tmp_path: Path) -> None:
    class StoppedProcessManager:
        def health(self, name: str) -> ProcessState:
            return ProcessState(name, "STOPPED")

    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "ok\n", "")

    status = Doctor(
        paths=paths,
        command_runner=runner,
        process_manager=StoppedProcessManager(),
    ).run(DoctorOptions(check_optional_components=False))
    supervisor = status.component("Supervisor Bridge")

    assert supervisor is not None
    assert supervisor.status == HealthStatus.DEGRADED
    assert supervisor.repairable is True
    assert supervisor.recommended_action == "start_supervisor"
    assert supervisor.user_message == "Development environment needs to start."


def test_bootstrap_user_view_hides_internal_repair_names(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    from codex_supervisor_bridge.bootstrap import BootstrapService

    status = BootstrapService(paths=paths).repair_and_status(project_directory=tmp_path)
    rendered = json.dumps(status.user_view(), ensure_ascii=False).lower()
    assert "devspace" not in rendered
    assert "mcp" not in rendered
    assert "start_process:" not in rendered


def test_devspace_bootstrap_writes_scoped_v1_config_and_process_command(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    project = tmp_path / "project"
    project.mkdir()
    bootstrap = DevSpaceBootstrap.from_app_data(paths, port=39101, project_directory=project)
    config_path = bootstrap.write_config()
    document = json.loads(config_path.read_text(encoding="utf-8"))
    assert document["host"] == "127.0.0.1"
    assert document["port"] == 39101
    assert str(project) in document["allowedRoots"]
    assert not (bootstrap.config_directory / "auth.json").exists()
    assert bootstrap.process_spec().command == ["devspace", "serve"]


def test_local_codex_bridge_bootstrap_checks_current_control_surface() -> None:
    bootstrap = LocalCodexBridgeBootstrap(
        LocalCodexBridgeBootstrapConfig(launch_command=["node", "bridge.js"])
    )
    missing = bootstrap.protocol_health({"codex_turn", "codex_observe"})
    assert missing.status == HealthStatus.DEGRADED
    assert missing.repairable is True
    ready = bootstrap.protocol_health(
        {"codex_turn", "codex_observe", "codex_steer", "codex_respond", "codex_interrupt"}
    )
    assert ready.status == HealthStatus.READY
    assert ready.advanced["semantics"] == ["turn", "observe", "steer", "respond", "interrupt"]


def test_local_codex_bridge_rejects_npm_start_and_uses_node_entrypoint(tmp_path: Path) -> None:
    for polluted in (
        ["npm", "start"],
        ["npm.cmd", "start"],
        ["npm", "run", "start"],
        [str(tmp_path / "npm.cmd"), "start"],
    ):
        with pytest.raises(ValidationError):
            LocalCodexBridgeBootstrapConfig(launch_command=polluted)

    build_root = tmp_path / "Local-Codex-Bridge"
    entrypoint = build_root / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    command = LocalCodexBridgeBootstrap.canonical_launch_command(build_root)

    assert command == ["node", str(entrypoint.resolve())]
    from_repository = LocalCodexBridgeBootstrap.from_repository(build_root, node_executable="C:/node.exe")
    assert from_repository.config.launch_command == ["C:/node.exe", str(entrypoint.resolve())]


def test_doctor_reads_local_codex_repository_launch(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    repository = tmp_path / "Local-Codex-Bridge"
    entrypoint = repository / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.local_codex_repository = repository
    config.advanced.executable_paths["node"] = "C:/Program Files/nodejs/node.exe"
    config_store.save(config)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "v24.9.0\n", "")

    doctor = Doctor(
        paths=paths,
        config_store=config_store,
        executable_finder=lambda name: "C:/Program Files/nodejs/node.exe" if name == "node" else None,
        command_runner=runner,
    )
    health = doctor._codex_control(config)

    assert health.status == HealthStatus.READY
    assert health.advanced["entrypoint"] == str(entrypoint.resolve())
    assert health.advanced["launch_command"] == [
        "C:/Program Files/nodejs/node.exe",
        str(entrypoint.resolve()),
    ]
    assert health.advanced["node_version"] == "v24.9.0"


def test_doctor_local_codex_repository_rejects_old_node(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    repository = tmp_path / "Local-Codex-Bridge"
    entrypoint = repository / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    config_store = ConfigStore(paths=paths)
    config = AppConfig.safe_defaults(paths)
    config.advanced.local_codex_repository = repository
    config.advanced.executable_paths["node"] = "C:/node-20/node.exe"
    config_store.save(config)

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "v20.19.0\n", "")

    doctor = Doctor(
        paths=paths,
        config_store=config_store,
        executable_finder=lambda name: "C:/node-20/node.exe" if name == "node" else None,
        command_runner=runner,
    )
    health = doctor._codex_control(config)

    assert health.status == HealthStatus.DEGRADED
    assert health.advanced["node_version"] == "v20.19.0"


def test_secret_store_round_trip_and_remote_access_security_gate() -> None:
    secrets = MemorySecretStore()
    secrets.set("chatgpt", "secret-value")
    assert secrets.get("chatgpt") == "secret-value"
    secrets.delete("chatgpt")
    assert secrets.get("chatgpt") is None

    errors = SecureRemoteAccessValidator.validate(
        SecureRemoteAccessConfig(
            public_url="http://0.0.0.0:8765/mcp",
            bind_host="0.0.0.0",
        )
    )
    assert "local MCP must bind to loopback" in errors
    assert "remote MCP URL must use HTTPS" in errors
    assert "remote MCP authentication is not configured" in errors

    assert SecureRemoteAccessValidator.validate(
        SecureRemoteAccessConfig(
            public_url="https://example.invalid/mcp",
            auth_secret_ref="chatgpt",
            session_identity="session-1",
        )
    ) == []

    remote = SecureRemoteAccessController()
    connected = remote.start(
        SecureRemoteAccessConfig(
            public_url="https://example.invalid/mcp",
            auth_secret_ref="chatgpt",
            session_identity="session-1",
        )
    )
    assert connected["status"] == "connected"
    remote.stop()
    assert remote.health()["active"] is False
    assert remote.reconnect()["status"] == "connected"
    assert remote.rotate(
        SecureRemoteAccessConfig(
            public_url="https://example.invalid/rotated",
            auth_secret_ref="chatgpt",
            session_identity="session-2",
        )
    )["session_identity"] == "session-2"


def test_secure_remote_controller_fails_closed_without_configuration() -> None:
    remote = SecureRemoteAccessController()
    with pytest.raises(RuntimeError, match="not been configured"):
        remote.reconnect()

    remote.start(
        SecureRemoteAccessConfig(
            public_url="https://example.invalid/mcp",
            auth_secret_ref="chatgpt",
            session_identity="session-1",
        )
    )
    remote.stop()

    assert remote.health()["active"] is False
    assert remote.health()["public_url"] == "https://example.invalid/mcp"
    assert remote.health()["session_identity"] == "session-1"
    with pytest.raises(ValueError, match="prerequisites failed"):
        remote.rotate(
            SecureRemoteAccessConfig(
                public_url="http://0.0.0.0:8765/mcp",
                bind_host="0.0.0.0",
                auth_secret_ref="chatgpt",
                session_identity="session-2",
            )
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI is only available on Windows")
def test_windows_dpapi_secret_store_round_trip(tmp_path: Path) -> None:
    from codex_supervisor_bridge.bootstrap.secrets import WindowsDpapiSecretStore

    store = WindowsDpapiSecretStore(tmp_path / "secrets")
    store.set("devspace-owner-token", "owner-secret-not-logged")

    assert store.get("devspace-owner-token") == "owner-secret-not-logged"
    store.delete("devspace-owner-token")
    assert store.get("devspace-owner-token") is None


def test_first_authorization_flow_keeps_credentials_out_of_result() -> None:
    secrets = MemorySecretStore()
    opened: list[str] = []
    flow = FirstAuthorizationFlow(
        "devspace",
        "https://example.invalid/authorize",
        secrets,
        browser_opener=lambda url: opened.append(url) or True,
    )
    challenge = flow.begin(secret_ref="workspace-auth")
    assert opened == [challenge.browser_url]
    result = flow.complete(state=challenge.state, credential="do-not-log", secret_ref=challenge.secret_ref)
    assert result.status == AuthorizationStatus.AUTHORIZED
    assert "do-not-log" not in result.model_dump_json()
    assert secrets.get("workspace-auth") == "do-not-log"


def test_devspace_oauth_tokens_are_secret_store_backed_and_redacted() -> None:
    async def scenario() -> None:
        secrets = MemorySecretStore()
        storage = SecretTokenStorage(secrets, secret_ref="devspace-oauth")
        token = OAuthToken(
            access_token="access-do-not-log",
            token_type="Bearer",
            refresh_token="refresh-do-not-log",
            expires_in=3600,
        )

        await storage.set_tokens(token)
        raw = secrets.get("devspace-oauth")
        restored = await storage.get_tokens()

        assert raw is not None
        assert "access-do-not-log" in raw
        assert restored == token
        rendered = json.dumps(redact_oauth_payload(token.model_dump()))
        assert "access-do-not-log" not in rendered
        assert "refresh-do-not-log" not in rendered
        rendered_client = json.dumps(
            redact_oauth_payload({"client_secret": "client-secret-do-not-log"})
        )
        assert "client-secret-do-not-log" not in rendered_client

    asyncio.run(scenario())


def test_devspace_local_oauth_uses_mcp_provider_and_owner_form() -> None:
    async def scenario() -> None:
        secrets = MemorySecretStore()
        secrets.set("devspace-owner-token", "owner-secret-not-logged")
        providers: list[object] = []

        class FakeResponse:
            def __init__(self, status_code: int, headers: dict[str, str]) -> None:
                self.status_code = status_code
                self.headers = headers

        class FakeClient:
            async def get(self, url: str, *, headers: dict[str, str] | None = None) -> FakeResponse:
                del url, headers
                return FakeResponse(200, {})

            async def post(self, url: str, *, data: dict[str, str], follow_redirects: bool = False) -> FakeResponse:
                del url, follow_redirects
                assert data == {"owner_token": "owner-secret-not-logged"}
                return FakeResponse(
                    302,
                    {"location": "http://127.0.0.1/codex-supervisor-callback?code=one-time-code&state=state-1"},
                )

        @asynccontextmanager
        async def factory(auth: object):
            if auth is not None:
                providers.append(auth)
            yield FakeClient()

        missing = await DevSpaceLocalOAuthDriver().authorize(
            mcp_url="http://127.0.0.1:39101/mcp",
            secret_store=MemorySecretStore(),
            http_client_factory=factory,
        )
        assert missing.status == "NEEDS_REPAIR"

        driver = DevSpaceLocalOAuthDriver(http_client_factory=factory)
        approval = await driver.submit_owner_approval(
            client=FakeClient(),
            authorization_url="http://127.0.0.1:39101/authorize?state=state-1",
            owner_token="owner-secret-not-logged",
        )
        assert approval.code == "one-time-code"
        assert approval.state == "state-1"

        authorized = await driver.authorize(
            mcp_url="http://127.0.0.1:39101/mcp",
            secret_store=secrets,
            http_client_factory=factory,
        )
        assert authorized.status == "AUTHORIZED"
        assert len(providers) == 1
        assert isinstance(providers[0], OAuthClientProvider)

    asyncio.run(scenario())


class _FakeDevSpaceOAuthHandler(BaseHTTPRequestHandler):
    authorize_calls = 0
    register_calls = 0
    token_calls = 0

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def _send_json(self, payload: dict[str, object], *, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        base_url = getattr(self.server, "base_url")
        if path == "/mcp":
            if self.headers.get("Authorization") == "Bearer fake-access-token":
                self._send_json({})
            else:
                self.send_response(401)
                self.send_header(
                    "WWW-Authenticate",
                    f'Bearer resource_metadata="{base_url}/.well-known/oauth-protected-resource", scope="devspace"',
                )
                self.end_headers()
        elif path == "/.well-known/oauth-protected-resource":
            self._send_json(
                {
                    "resource": f"{base_url}/mcp",
                    "authorization_servers": [base_url],
                    "scopes_supported": ["devspace"],
                }
            )
        elif path == "/.well-known/oauth-authorization-server":
            self._send_json(
                {
                    "issuer": base_url,
                    "authorization_endpoint": f"{base_url}/authorize",
                    "token_endpoint": f"{base_url}/token",
                    "registration_endpoint": f"{base_url}/register",
                    "scopes_supported": ["devspace"],
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                }
            )
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        if path == "/register":
            type(self).register_calls += 1
            registration = json.loads(body)
            self._send_json(
                {
                    "client_id": "fake-client",
                    "redirect_uris": registration.get("redirect_uris"),
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "scope": registration.get("scope"),
                },
                status=201,
            )
            return
        if path == "/authorize":
            type(self).authorize_calls += 1
            form = parse_qs(body)
            if form.get("owner_token", [""])[0] != "owner-secret-not-logged":
                self.send_error(403)
                return
            query = parse_qs(urlparse(self.path).query)
            state = query.get("state", [""])[0]
            redirect_uri = query.get(
                "redirect_uri",
                ["http://127.0.0.1/codex-supervisor-callback"],
            )[0]
            self.send_response(302)
            self.send_header("Location", f"{redirect_uri}?code=fake-code&state={state}")
            self.end_headers()
            return
        if path == "/token":
            type(self).token_calls += 1
            form = parse_qs(body)
            grant_type = form.get("grant_type", [""])[0]
            assert grant_type in {"authorization_code", "refresh_token"}
            self._send_json(
                {
                    "access_token": "fake-access-token",
                    "token_type": "Bearer",
                    "refresh_token": "fake-refresh-token",
                    "expires_in": 3600,
                    "scope": "devspace",
                }
            )
            return
        self.send_error(404)


def test_devspace_local_oauth_real_loopback_protocol() -> None:
    _FakeDevSpaceOAuthHandler.authorize_calls = 0
    _FakeDevSpaceOAuthHandler.register_calls = 0
    _FakeDevSpaceOAuthHandler.token_calls = 0
    secrets = MemorySecretStore()
    secrets.set("devspace-owner-token", "owner-secret-not-logged")
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeDevSpaceOAuthHandler)
    server.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = asyncio.run(
            DevSpaceLocalOAuthDriver().authorize(
                mcp_url=f"{server.base_url}/mcp",
                secret_store=secrets,
            )
        )
        resumed = asyncio.run(
            DevSpaceLocalOAuthDriver().authorize(
                mcp_url=f"{server.base_url}/mcp",
                secret_store=secrets,
            )
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.status == "AUTHORIZED"
    stored = json.loads(secrets.get("devspace-oauth") or "{}")
    assert stored["access_token"] == "fake-access-token"
    assert stored["refresh_token"] == "fake-refresh-token"
    assert _FakeDevSpaceOAuthHandler.authorize_calls == 1
    assert _FakeDevSpaceOAuthHandler.register_calls == 1
    assert _FakeDevSpaceOAuthHandler.token_calls == 1
    stored_client = asyncio.run(
        SecretTokenStorage(secrets, secret_ref="devspace-oauth").get_client_info()
    )
    assert isinstance(stored_client, OAuthClientInformationFull)
    assert stored_client.client_id == "fake-client"

    assert resumed.status == "AUTHORIZED"
    assert _FakeDevSpaceOAuthHandler.authorize_calls == 1
    assert _FakeDevSpaceOAuthHandler.register_calls == 1
    assert _FakeDevSpaceOAuthHandler.token_calls == 1

    redacted = json.dumps(redact_oauth_payload(stored))
    assert "fake-access-token" not in redacted
    assert "fake-refresh-token" not in redacted


def test_codex_readiness_requires_more_than_an_executable() -> None:
    def runner(command: list[str], **kwargs: object) -> object:
        del kwargs
        return subprocess.CompletedProcess(command, 0, "codex 1.2.3\n", "")

    detector = CodexReadinessDetector(finder=lambda name: "C:/tools/codex.exe", runner=runner)
    readiness = detector.probe()
    assert readiness.process_launchable is True
    assert readiness.authentication_ready is None
    assert readiness.status == HealthStatus.DEGRADED


def test_command_session_latches_unknown_interrupt_for_reconciliation() -> None:
    session = CommandSession(task_id="task-1", command_id="command-1", pid=42)
    session.interrupt(acknowledged=False)
    assert session.status == CommandSessionStatus.UNKNOWN
    session.require_reconciliation()
    assert session.status == CommandSessionStatus.RECONCILIATION_REQUIRED
    assert session.stdin_open is False


def test_runtime_recovery_fails_closed_for_orphaned_codex_writer() -> None:
    from codex_supervisor_bridge.bootstrap import RecoveryStatus, RuntimeRecovery

    decision = RuntimeRecovery.decide(
        active_writer=ActiveWriter.CODEX,
        runtime_present=False,
        task_state_present=True,
    )
    assert decision.status == RecoveryStatus.RECONCILIATION_REQUIRED
    assert decision.requires_user_action is True


def test_command_authorization_is_workspace_and_revision_fenced(tmp_path: Path) -> None:
    request = CommandRequest(
        task_id="task-1",
        command="pytest",
        cwd=tmp_path,
        workspace_root=tmp_path,
        expected_revision=3,
        current_revision=3,
        writer=ActiveWriter.CHATGPT,
        writer_epoch=1,
        policy=CommandAuthorizationPolicy.ASK,
    )
    assert authorize_command(request).verdict == CommandVerdict.ASK
    assert authorize_command(request.model_copy(update={"approved": True})).verdict == CommandVerdict.ALLOW
    assert authorize_command(request.model_copy(update={"cwd": tmp_path.parent})).verdict == CommandVerdict.DENY
    dangerous = request.model_copy(update={"command": "git reset --hard"})
    assert authorize_command(dangerous).verdict == CommandVerdict.DENY
    for command in (
        "rm -rf .",
        "rmdir /s /q .",
        "del /s /q *",
        "Remove-Item . -Recurse -Force",
        "bcdedit /delete {current}",
    ):
        assert authorize_command(request.model_copy(update={"command": command})).verdict == CommandVerdict.DENY


def test_profile_ab_harness_compares_normalized_supervisor_semantics() -> None:
    steps = list(HarnessStep)

    def trace(profile: str) -> HarnessTrace:
        return HarnessTrace(
            profile=profile,
            task_id="task-1",
            workspace_identity="provider-specific-workspace",
            steps=steps,
            revisions=[0, 1, 2, 3],
            writer_history=["NONE", "CHATGPT", "NONE", "CODEX", "NONE", "CHATGPT"],
            evidence=["diff", "checkpoint", "final-git-state"],
        )

    comparison = ProfileABHarness(lambda: trace("A"), lambda: trace("B")).run()
    assert comparison.equivalent is True
    assert comparison.differences == []


def test_profile_scenario_runner_executes_all_required_steps_without_raw_payloads() -> None:
    class FakeDriver:
        def execute(self, step: HarnessStep, *, task_id: str) -> ScenarioObservation:
            return ScenarioObservation(
                task_id=task_id,
                workspace_identity=f"workspace-{step.value}",
                revision=list(HarnessStep).index(step),
                writer="CODEX" if step in {HarnessStep.CODEX_EXECUTION, HarnessStep.OBSERVE} else "CHATGPT",
                evidence=[step.value],
            )

    trace = ProfileScenarioRunner().run(profile="A", task_id="task-1", driver=FakeDriver())
    assert trace.steps == list(HarnessStep)
    assert len(trace.evidence) == len(HarnessStep)
    assert not hasattr(trace, "raw_payload")


def test_profile_ab_harness_reports_normalized_differences() -> None:
    def trace(
        profile: str,
        *,
        task_id: str = "task-1",
        revisions: list[int] | None = None,
        writer_history: list[str] | None = None,
        evidence: list[str] | None = None,
    ) -> HarnessTrace:
        return HarnessTrace(
            profile=profile,
            task_id=task_id,
            workspace_identity="provider-specific-workspace",
            steps=list(HarnessStep),
            revisions=revisions or [0, 1, 2, 3],
            writer_history=writer_history or ["NONE", "CHATGPT", "NONE", "CODEX"],
            evidence=evidence or ["diff", "checkpoint", "final-git-state"],
        )

    identity = ProfileABHarness(
        lambda: trace("A", task_id="task-a"),
        lambda: trace("B", task_id="task-b"),
    ).run()
    assert identity.equivalent is False
    assert "task identity changed" in identity.differences

    semantics = ProfileABHarness(
        lambda: trace("A", revisions=[0, 1, 2, 3]),
        lambda: trace("B", revisions=[0, 1, 2, 4]),
    ).run()
    assert semantics.equivalent is False
    assert "normalized Supervisor semantics differ" in semantics.differences

    evidence = ProfileABHarness(
        lambda: trace("A", evidence=["diff", "checkpoint"]),
        lambda: trace("B", evidence=["diff", "checkpoint", "extra"]),
    ).run()
    assert evidence.equivalent is False
    assert "normalized Supervisor semantics differ" in evidence.differences


def test_profile_scenario_runner_rejects_identity_and_status_changes() -> None:
    class SwappingDriver:
        def execute(self, step: HarnessStep, *, task_id: str) -> ScenarioObservation:
            del step, task_id
            return ScenarioObservation(task_id="other-task", workspace_identity="w", revision=0)

    with pytest.raises(ValueError, match="canonical task identity"):
        ProfileScenarioRunner().run(profile="A", task_id="task-1", driver=SwappingDriver())

    class FailedDriver:
        def execute(self, step: HarnessStep, *, task_id: str) -> ScenarioObservation:
            failed = step == HarnessStep.DIRECT_PATCH
            return ScenarioObservation(
                task_id=task_id,
                workspace_identity="w",
                status="failed" if failed else "ok",
                revision=list(HarnessStep).index(step),
                writer="CHATGPT",
                evidence=[],
            )

    with pytest.raises(RuntimeError, match="revision 3"):
        ProfileScenarioRunner().run(profile="B", task_id="task-1", driver=FailedDriver())


class SlowStopProcess(DummyProcess):
    def __init__(self) -> None:
        super().__init__()
        self.kill_calls = 0
        self._killed = False

    def terminate(self) -> None:
        return None

    def kill(self) -> None:
        self.kill_calls += 1
        self._killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        if not self._killed:
            raise subprocess.TimeoutExpired(cmd=["dummy"], timeout=timeout or 0)
        return int(self.returncode)


def test_process_manager_does_not_launch_duplicate_while_running(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    spec = ManagedProcessSpec(name="bridge", command=["dummy"])

    first = manager.start(spec)
    second = manager.start(spec)

    assert first.status == "RUNNING"
    assert second is first
    assert len(launched) == 1


def test_process_manager_detects_crash_on_next_health_check(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    spec = ManagedProcessSpec(name="bridge", command=["dummy"])

    assert manager.start(spec).status == "RUNNING"
    launched[0].returncode = 1
    crashed = manager.health("bridge")

    assert crashed.status == "CRASHED"
    assert crashed.last_exit == 1
    assert manager.health("bridge").status == "CRASHED"
    repaired = manager.repair_stale("bridge")
    assert repaired.status == "STOPPED"
    assert not (tmp_path / "runtime" / "bridge.lock").exists()


def test_process_manager_restart_limit_is_bounded(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        process.returncode = 1
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    spec = ManagedProcessSpec(name="bridge", command=["dummy"], max_restarts=2)

    assert manager.start(spec).status == "CRASHED"
    assert manager.restart(spec).restart_count == 1
    assert manager.restart(spec).restart_count == 2
    blocked = manager.restart(spec)

    assert blocked.status == "UNAVAILABLE"
    assert blocked.restart_count == 3
    assert blocked.technical_detail == "restart limit reached"
    assert len(launched) == 3


def test_process_manager_restart_reuses_log_and_persists_count(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    spec = ManagedProcessSpec(name="bridge", command=["dummy"])

    running = manager.start(spec)
    restarted = manager.restart(spec)

    assert restarted.status == "RUNNING"
    assert restarted.log_path == running.log_path
    assert restarted.restart_count == 1
    persisted = json.loads((tmp_path / "runtime" / "processes.json").read_text(encoding="utf-8"))
    assert persisted["bridge"]["restart_count"] == 1
    assert persisted["bridge"]["pid"] == restarted.pid


def test_process_manager_readiness_probe_can_succeed(tmp_path: Path) -> None:
    launched: list[DummyProcess] = []

    def launch(*args: object, **kwargs: object) -> DummyProcess:
        del args, kwargs
        process = DummyProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    state = manager.start(
        ManagedProcessSpec(
            name="bridge",
            command=["dummy"],
            readiness_probe=lambda: True,
        )
    )

    assert state.status == "RUNNING"
    assert state.pid == launched[0].pid


def test_process_manager_graceful_stop_falls_back_to_hard_kill(tmp_path: Path) -> None:
    launched: list[SlowStopProcess] = []

    def launch(*args: object, **kwargs: object) -> SlowStopProcess:
        del args, kwargs
        process = SlowStopProcess()
        launched.append(process)
        return process

    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs", launcher=launch)
    assert manager.start(ManagedProcessSpec(name="bridge", command=["dummy"])).status == "RUNNING"
    stopped = manager.stop("bridge")

    assert stopped.status == "STOPPED"
    assert stopped.last_exit == -9
    assert launched[0].kill_calls == 1
    assert not (tmp_path / "runtime" / "bridge.lock").exists()


def test_codex_readiness_full_probe_matrix(tmp_path: Path) -> None:
    def completed(
        command: list[str],
        *,
        stdout: str = "",
        stderr: str = "",
        returncode: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    calls: list[list[str]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del kwargs
        calls.append(command)
        return completed(command)

    missing = CodexReadinessDetector(finder=lambda name: None, runner=runner).probe()
    assert missing.status == HealthStatus.UNAVAILABLE
    assert missing.process_launchable is False
    assert missing.technical_detail == "executable not found"
    assert calls == []

    version_failed = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=lambda command, **kwargs: completed(command, returncode=1, stderr="panic"),
    ).probe()
    assert version_failed.status == HealthStatus.DEGRADED
    assert version_failed.process_launchable is False
    assert version_failed.user_message == "Codex needs sign-in or runtime repair."

    def raising_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        del command, kwargs
        raise OSError("cannot launch")

    launch_failed = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=raising_runner,
    ).probe()
    assert launch_failed.status == HealthStatus.DEGRADED
    assert launch_failed.process_launchable is False
    assert "cannot launch" in (launch_failed.technical_detail or "")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()

    not_logged_in = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=lambda command, **kwargs: completed(
            command,
            stdout="codex 1.2.3\n",
            stderr="not logged in\n",
        ),
    ).probe(workspace=workspace)
    assert not_logged_in.status == HealthStatus.DEGRADED
    assert not_logged_in.authentication_ready is False
    assert not_logged_in.workspace_ready is True

    no_git = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=lambda command, **kwargs: completed(command, stdout="codex 1.2.3\n"),
    ).probe(workspace=tmp_path / "not-a-git-project")
    assert no_git.status == HealthStatus.DEGRADED
    assert no_git.workspace_ready is False
    assert no_git.user_message == "Codex is ready, but the selected project is unavailable."

    ready = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=lambda command, **kwargs: completed(command, stdout="codex 1.2.3\nlogged in\n"),
    ).probe(workspace=workspace)
    assert ready.status == HealthStatus.READY
    assert ready.process_launchable is True
    assert ready.authentication_ready is True
    assert ready.workspace_ready is True
    assert ready.version == "codex 1.2.3"
    assert ready.executable == "C:/tools/codex.exe"

    explicit = tmp_path / "codex.exe"
    explicit.write_text("placeholder", encoding="utf-8")
    explicit_probe = CodexReadinessDetector(
        finder=lambda name: None,
        runner=lambda command, **kwargs: completed(command, stdout="codex 9.9.9\nready\n"),
    ).probe(executable=str(explicit), workspace=workspace)
    assert explicit_probe.status == HealthStatus.READY
    assert explicit_probe.executable == str(explicit)


def test_configure_cli_persists_user_intent_end_to_end(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    from codex_supervisor_bridge.bootstrap.configuration import CommandPolicy, DevelopmentStyle

    app = tmp_path / "app"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_SUPERVISOR_DATA_DIR", str(app))
    monkeypatch.setenv("SUPERVISOR_DB_PATH", str(app / "data" / "supervisor.db"))

    cli_main(
        [
            "configure",
            "--project",
            str(project),
            "--style",
            "web_first",
            "--command-policy",
            "ASK",
            "--no-allow-codex-delegation",
            "--auto-commit",
            "--no-auto-pr",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    config = ConfigStore(paths=AppDataPaths.from_environment()).load().config
    assert payload["project_directory"] == str(project.resolve())
    assert config.basic.project_directory == project.resolve()
    assert config.basic.development_style == DevelopmentStyle.WEB_FIRST
    assert config.basic.local_command_policy == CommandPolicy.ASK
    assert config.basic.allow_chatgpt_codex_delegation is False
    assert config.basic.automatic_git_commit is True
    assert config.basic.automatic_pull_request is False
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "devspace" not in rendered
    assert "local-codex-bridge" not in rendered


def test_configure_cli_advanced_json_keeps_gui_diagnostics(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    app = tmp_path / "app"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_SUPERVISOR_DATA_DIR", str(app))
    monkeypatch.setenv("SUPERVISOR_DB_PATH", str(app / "data" / "supervisor.db"))

    cli_main(
        [
            "configure",
            "--project",
            str(project),
            "--advanced",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["project_directory"] == str(project.resolve())
    assert "selected_profile" in payload
    assert "diagnostics" in payload
    assert any(item["capability"] == "Project directory" for item in payload["diagnostics"])


def test_doctor_cli_emits_structured_ux_without_provider_names(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    app = tmp_path / "app"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_SUPERVISOR_DATA_DIR", str(app))
    monkeypatch.setenv("SUPERVISOR_DB_PATH", str(app / "data" / "supervisor.db"))

    cli_main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    rendered = json.dumps(payload, ensure_ascii=False).lower()
    assert "components" in payload
    assert "project_directory" in payload
    assert "devspace" not in rendered
    assert "local-codex-bridge" not in rendered
    assert "codex-control-plane" not in rendered
    assert "sqlite" not in rendered
