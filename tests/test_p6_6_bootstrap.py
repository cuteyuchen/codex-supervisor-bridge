from __future__ import annotations

import asyncio
import json
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from mcp.client.auth.oauth2 import OAuthClientProvider
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
    ConfigStore,
    DevSpaceBootstrap,
    DevSpaceLocalOAuthDriver,
    DevSpaceVersionCompatibility,
    Doctor,
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
from codex_supervisor_bridge.mcp.server import _is_loopback_host
from codex_supervisor_bridge.memory.models import ActiveWriter


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
    assert not (Path.home() / ".devspace" / "config.json").exists()


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
    ).start(project_directory=tmp_path)

    local = next(item for item in process_manager.started if item.name == "local_codex_bridge")
    assert list(local.command) == ["node", "bridge.js"]
    assert any(item.action == "start_process:local_codex_bridge" for item in result.repairs)


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
    with pytest.raises(ValidationError):
        LocalCodexBridgeBootstrapConfig(launch_command=["npm", "start"])

    build_root = tmp_path / "Local-Codex-Bridge"
    entrypoint = build_root / "dist" / "src" / "index.js"
    entrypoint.parent.mkdir(parents=True)
    entrypoint.write_text("placeholder", encoding="utf-8")
    command = LocalCodexBridgeBootstrap.canonical_launch_command(build_root)

    assert command == ["node", str(entrypoint.resolve())]


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
