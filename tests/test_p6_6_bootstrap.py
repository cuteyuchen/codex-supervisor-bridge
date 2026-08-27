from __future__ import annotations

import json
import subprocess
from pathlib import Path

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
    SecureRemoteAccessConfig,
    SecureRemoteAccessController,
    SecureRemoteAccessValidator,
    authorize_command,
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
    assert document["configVersion"] == 1
    assert document["server"]["host"] == "127.0.0.1"
    assert document["server"]["port"] == 39101
    assert document["tools"]["mode"] == "codex"
    assert str(project) in document["workspaces"]["allowedRoots"]
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
