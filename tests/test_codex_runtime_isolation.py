from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

import codex_supervisor_bridge.bootstrap.lcb_runtime_proxy as lcb_runtime_proxy
from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    PendingInteraction,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.bootstrap.codex_isolation import (
    CodexRuntimeMetadata,
    LcbRuntimeIsolationUnsupportedError,
    ProcessObservation,
    RuntimeOwnershipError,
    SupervisorCodexRuntimeManager,
    runtime_process_chain_failure,
)
from codex_supervisor_bridge.bootstrap.lcb_hardening import (
    LCB_HARDENING_REVISION,
    LCB_RUNTIME_CONTRACT,
)
from codex_supervisor_bridge.bootstrap.process import (
    CodexProcessOwnership,
    ManagedProcessSpec,
    ProcessManager,
    ProcessState,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeAffinityError,
    CodexRuntimeCircuitOpenError,
    assert_runtime_affinity,
    bind_codex_runtime,
    close_runtime_circuit_after_recovery,
    get_codex_runtime,
    open_runtime_circuit,
    record_runtime_observation,
)
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_session import (
    AgentSessionManager,
    AgentSessionUnavailableError,
)
from codex_supervisor_bridge.supervisor.runtime_resolver import RuntimeResolver


def _observation(
    pid: int,
    *,
    parent_pid: int | None,
    executable: str,
    app_server: bool = False,
    parent_executable: str = "python.exe",
) -> ProcessObservation:
    return ProcessObservation(
        pid=pid,
        creation_time=f"created-{pid}",
        executable=executable,
        command_line_fingerprint=f"fingerprint-{pid}",
        parent_pid=parent_pid,
        parent_creation_time=f"created-{parent_pid}" if parent_pid else None,
        parent_executable=parent_executable if parent_pid else None,
        app_server_stdio=app_server,
    )


def _verified_metadata(
    root: Path,
    *,
    instance_id: str = "csb-codex-test",
    epoch: int = 1,
    desktop_pid: int = 200,
    app_server_pid: int = 102,
) -> CodexRuntimeMetadata:
    runtime = root / "runtime" / "codex" / instance_id
    metadata = CodexRuntimeMetadata(
        instance_id=instance_id,
        runtime_epoch=epoch,
        lcb_runtime_contract=LCB_RUNTIME_CONTRACT,
        lcb_hardening_revision=LCB_HARDENING_REVISION,
        ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        ownership_token_hash=hashlib.sha256(b"owned-token").hexdigest(),
        status="READY",
        runtime_directory=str(runtime),
        codex_home=str(runtime / "home"),
        started_at="2026-08-30T00:00:00+00:00",
        supervisor_parent_pid=99,
        proxy_process=_observation(100, parent_pid=99, executable="python.exe"),
        lcb_process=_observation(101, parent_pid=100, executable="node.exe"),
        app_server_process=_observation(
            app_server_pid,
            parent_pid=101,
            executable="codex.exe",
            app_server=True,
            parent_executable="node.exe",
        ),
        desktop_processes=[
            _observation(
                desktop_pid,
                parent_pid=50,
                executable="codex.exe",
                app_server=True,
                parent_executable="ChatGPT.exe",
            )
        ],
        desktop_runtime_present=True,
    )
    manager = SupervisorCodexRuntimeManager(root)
    return manager.verify_metadata(metadata)


def _bind_verified_runtime(
    memory: MemoryService,
    task_id: str,
    *,
    instance_id: str = "csb-codex-one",
    epoch: int = 1,
    status: str = "planning",
) -> PlanHandle:
    task = memory.create_task(task_id, "runtime isolation", repository="C:/repo")
    _, runtime = bind_codex_runtime(
        memory.store,
        task_id,
        task.revision,
        event_type=EventType.CODEX_STARTED,
        thread_id="thread-one",
        turn_id="turn-one",
        remote_status=status,
        task_phase=TaskPhase.PLANNING,
        runtime_instance_id=instance_id,
        runtime_epoch=epoch,
        runtime_ownership="SUPERVISOR_MANAGED",
        isolation_verified=True,
        interrupt_attempted=False,
    )
    return PlanHandle(
        task_id=task_id,
        thread_id=runtime.thread_id,
        turn_id=runtime.turn_id,
        runtime_instance_id=instance_id,
        runtime_epoch=epoch,
        runtime_ownership="SUPERVISOR_MANAGED",
        isolation_verified=True,
        status=status,
    )


class _FakeRuntimeManager:
    def __init__(self, metadata: CodexRuntimeMetadata) -> None:
        self.metadata = metadata
        self.degraded: list[str] = []

    def refresh(self) -> CodexRuntimeMetadata:
        return self.metadata

    def wait_until_verified(self) -> CodexRuntimeMetadata:
        return self.metadata

    def public_status(self) -> dict[str, Any]:
        return self.metadata.public_status()

    def mark_degraded(self, code: str, detail: str) -> CodexRuntimeMetadata:
        self.degraded.append(f"{code}:{detail}")
        self.metadata = self.metadata.model_copy(
            update={
                "status": "DEGRADED",
                "isolation_verified": False,
                "failure_code": code,
                "technical_detail": detail,
            }
        )
        return self.metadata

    def assert_destructive_lifecycle_allowed(self) -> None:
        if not self.metadata.isolation_verified:
            raise RuntimeError("ownership unknown")

    def replace(self) -> CodexRuntimeMetadata:
        self.metadata = self.metadata.model_copy(
            update={
                "instance_id": "csb-codex-replacement",
                "runtime_epoch": self.metadata.runtime_epoch + 1,
                "status": "READY",
                "isolation_verified": True,
            }
        )
        return self.metadata

    def mark_stopped(self) -> CodexRuntimeMetadata:
        self.metadata = self.metadata.model_copy(
            update={"status": "STOPPED", "isolation_verified": False}
        )
        return self.metadata


class _FakeBackend:
    def __init__(self) -> None:
        self.start_calls = 0
        self.execution_calls = 0
        self.interrupt_calls = 0
        self.respond_calls = 0
        self.resume_calls = 0

    async def __aenter__(self) -> "_FakeBackend":
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="local_codex_bridge",
            status=BackendHealthStatus.READY,
            user_message="ready",
        )

    async def start_plan(self, **_kwargs: Any) -> PlanHandle:
        self.start_calls += 1
        return PlanHandle(thread_id="thread-new", turn_id="turn-new", status="planning")

    async def start_execution(self, **_kwargs: Any) -> PlanHandle:
        self.execution_calls += 1
        return PlanHandle(thread_id="thread-exec", turn_id="turn-exec", status="executing")

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)

    async def observe(self, handle: PlanHandle, **_kwargs: Any) -> AgentSnapshot:
        return AgentSnapshot(
            status=handle.status,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )

    async def steer(self, handle: PlanHandle, *_args: Any, **_kwargs: Any) -> AgentSnapshot:
        return await self.observe(handle)

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        self.interrupt_calls += 1
        return AgentSnapshot(
            status="interrupted",
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )

    async def list_pending_interactions(self, _handle: PlanHandle) -> list[PendingInteraction]:
        return []

    async def respond_interaction(
        self,
        handle: PlanHandle,
        _interaction: PendingInteraction,
        _response: dict[str, Any],
    ) -> AgentSnapshot:
        self.respond_calls += 1
        return await self.observe(handle)

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        self.resume_calls += 1
        return await self.observe(handle)


def test_runtime_namespace_is_unique_and_does_not_copy_auth_or_mcp_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_home = tmp_path / "user-codex"
    source_home.mkdir()
    source_config = source_home / "config.toml"
    source_config.write_text(
        """
model = "gpt-5.6-luna"
model_provider = "third_party"

[model_providers.third_party]
name = "Third Party"
base_url = "http://127.0.0.1:3000"
wire_api = "responses"
env_key = "THIRD_PARTY_KEY"
http_headers = { Authorization = "secret-provider-value" }

[mcp_servers.desktop_only]
url = "http://127.0.0.1:3000/mcp?token=secret-mcp-value"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    (source_home / "auth.json").write_text(
        '{"token":"secret-auth-value"}\n',
        encoding="utf-8",
    )
    before = hashlib.sha256(source_config.read_bytes()).hexdigest()
    parsed_payloads: list[str] = []
    original_toml_loads = __import__("tomllib").loads

    def tracked_toml_loads(payload: str) -> dict[str, Any]:
        parsed_payloads.append(payload)
        return original_toml_loads(payload)

    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.codex_isolation.tomllib.loads",
        tracked_toml_loads,
    )
    manager = SupervisorCodexRuntimeManager(
        tmp_path / "bridge",
        uuid_factory=lambda: UUID("00000000-0000-0000-0000-000000000001"),
    )

    first = manager.prepare({"CODEX_HOME": str(source_home), "THIRD_PARTY_KEY": "in-env"})
    isolated_config = Path(first.codex_home) / "config.toml"
    rendered = isolated_config.read_text(encoding="utf-8")

    assert first.instance_id == "csb-codex-00000000-0000-0000-0000-000000000001"
    assert first.runtime_epoch == 1
    assert Path(first.codex_home).parent == Path(first.runtime_directory)
    assert "third_party" in rendered
    assert "desktop_only" not in rendered
    assert "secret-provider-value" not in rendered
    assert "secret-mcp-value" not in rendered
    assert not (Path(first.codex_home) / "auth.json").exists()
    assert hashlib.sha256(source_config.read_bytes()).hexdigest() == before
    assert "secret-auth-value" not in json.dumps(manager.advanced_status())
    assert "secret-provider-value" not in json.dumps(manager.advanced_status())
    assert "secret-provider-value" not in "\n".join(parsed_payloads)
    assert "secret-mcp-value" not in "\n".join(parsed_payloads)


def test_runtime_epoch_increments_and_old_namespace_is_not_reused(tmp_path: Path) -> None:
    source_home = tmp_path / "empty-home"
    source_home.mkdir()
    values = iter(
        [
            UUID("00000000-0000-0000-0000-000000000001"),
            UUID("00000000-0000-0000-0000-000000000002"),
        ]
    )
    manager = SupervisorCodexRuntimeManager(
        tmp_path / "bridge",
        uuid_factory=lambda: next(values),
    )
    first = manager.prepare({"CODEX_HOME": str(source_home)})
    second = manager.replace({"CODEX_HOME": str(source_home)})

    assert second.runtime_epoch == first.runtime_epoch + 1
    assert second.instance_id != first.instance_id
    assert second.runtime_directory != first.runtime_directory


def test_runtime_metadata_namespace_cannot_pivot_after_prepare(tmp_path: Path) -> None:
    source_home = tmp_path / "empty-home"
    source_home.mkdir()
    manager = SupervisorCodexRuntimeManager(
        tmp_path / "bridge",
        uuid_factory=lambda: UUID("00000000-0000-0000-0000-000000000003"),
    )
    manager.prepare({"CODEX_HOME": str(source_home)})
    original_path = manager.metadata_path
    payload = json.loads(original_path.read_text(encoding="utf-8"))
    payload["runtime_directory"] = str(tmp_path / "forged-runtime")
    original_path.write_text(json.dumps(payload), encoding="utf-8")

    refreshed = manager.refresh()

    assert refreshed.failure_code == "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"
    assert refreshed.isolation_verified is False
    assert manager.metadata_path == original_path


def test_unsafe_provider_overlay_failure_remains_isolation_unsupported(tmp_path: Path) -> None:
    source_home = tmp_path / "broken-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        'model_provider = "unterminated\n',
        encoding="utf-8",
    )
    manager = SupervisorCodexRuntimeManager(tmp_path / "bridge")

    with pytest.raises(LcbRuntimeIsolationUnsupportedError):
        manager.prepare({"CODEX_HOME": str(source_home)})

    assert manager.metadata is not None
    assert manager.metadata.failure_code == "LCB_RUNTIME_ISOLATION_UNSUPPORTED"
    assert manager.metadata_path.is_file()
    with pytest.raises(LcbRuntimeIsolationUnsupportedError):
        manager.prepare({"CODEX_HOME": str(source_home)})


def test_provider_overlay_supports_quoted_provider_table(tmp_path: Path) -> None:
    source_home = tmp_path / "quoted-provider-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text(
        """
model = "gpt-5.6-luna"
model_provider = "third.party"

[model_providers."third.party"]
name = "Third Party"
base_url = "http://127.0.0.1:3000"
wire_api = "responses"
env_key = "THIRD_PARTY_KEY"
""".strip()
        + "\n",
        encoding="utf-8",
    )
    manager = SupervisorCodexRuntimeManager(tmp_path / "bridge")

    metadata = manager.prepare({"CODEX_HOME": str(source_home)})
    rendered = (Path(metadata.codex_home) / "config.toml").read_text(encoding="utf-8")
    parsed = __import__("tomllib").loads(rendered)

    assert parsed["model_provider"] == "third.party"
    assert parsed["model_providers"]["third.party"]["env_key"] == "THIRD_PARTY_KEY"


def test_session_start_reports_isolation_unsupported_without_starting_backend(
    tmp_path: Path,
) -> None:
    source_home = tmp_path / "broken-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text("[broken\n", encoding="utf-8")
    runtime_manager = SupervisorCodexRuntimeManager(tmp_path / "bridge")
    memory = MemoryService()
    backend_created = False

    def factory() -> _FakeBackend:
        nonlocal backend_created
        runtime_manager.prepare({"CODEX_HOME": str(source_home)})
        backend_created = True
        return _FakeBackend()

    session = AgentSessionManager(
        memory,
        factory,
        runtime_manager=runtime_manager,
    )
    try:
        asyncio.run(session.start())

        assert session.connected is False
        assert backend_created is False
        assert session.runtime_error is not None
        assert session.runtime_error.startswith("LCB_RUNTIME_ISOLATION_UNSUPPORTED:")
    finally:
        memory.close()


def test_desktop_and_supervisor_app_server_identity_must_be_different(tmp_path: Path) -> None:
    safe = _verified_metadata(tmp_path)
    shared = _verified_metadata(tmp_path, desktop_pid=102, app_server_pid=102)

    assert safe.isolation_verified is True
    assert safe.app_server_process.pid != safe.desktop_processes[0].pid  # type: ignore[union-attr]
    assert shared.isolation_verified is False
    assert shared.failure_code == "UNSAFE_SHARED_CODEX_RUNTIME"


def test_runtime_verification_rejects_wrong_lcb_hardening_contract(tmp_path: Path) -> None:
    metadata = _verified_metadata(tmp_path)
    manager = SupervisorCodexRuntimeManager(tmp_path)

    wrong_contract = manager.verify_metadata(
        metadata.model_copy(update={"lcb_runtime_contract": "unsupported-contract"})
    )
    wrong_revision = manager.verify_metadata(
        metadata.model_copy(update={"lcb_hardening_revision": "unknown-revision"})
    )

    assert wrong_contract.isolation_verified is False
    assert wrong_contract.failure_code == "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"
    assert wrong_revision.isolation_verified is False
    assert wrong_revision.failure_code == "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"


class _DummyProcess:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.returncode or 0


class _ProxyChild:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired("node", timeout)
        return self.returncode


def _run_proxy_startup_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    replace_child_identity: bool,
) -> tuple[int, _ProxyChild, CodexRuntimeMetadata]:
    token = "owned-token"
    proxy_pid = os.getpid()
    child_pid = proxy_pid + 1000
    proxy = _observation(
        proxy_pid,
        parent_pid=99,
        executable="python.exe",
    )
    child_identity = _observation(
        child_pid,
        parent_pid=proxy_pid,
        executable="node.exe",
        parent_executable="python.exe",
    )
    replacement_identity = child_identity.model_copy(
        update={"creation_time": "pid-reused"}
    )
    metadata = CodexRuntimeMetadata(
        instance_id="csb-codex-proxy-timeout",
        runtime_epoch=1,
        lcb_runtime_contract=LCB_RUNTIME_CONTRACT,
        lcb_hardening_revision=LCB_HARDENING_REVISION,
        ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        ownership_token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        runtime_directory=str(tmp_path / "runtime" / "csb-codex-proxy-timeout"),
        codex_home=str(tmp_path / "runtime" / "csb-codex-proxy-timeout" / "home"),
        started_at="2026-08-30T00:00:00+00:00",
        supervisor_parent_pid=99,
    )
    metadata_path = tmp_path / "runtime.json"
    metadata_path.write_text(
        json.dumps(metadata.model_dump(mode="json")),
        encoding="utf-8",
    )
    child = _ProxyChild(child_pid)

    class _Inspector:
        def identity(self, pid: int) -> ProcessObservation | None:
            if pid == proxy_pid:
                return proxy
            if pid == child_pid:
                return replacement_identity if replace_child_identity else child_identity
            return None

        def snapshot(self) -> list[ProcessObservation]:
            return [proxy, child_identity]

    ticks = iter([0.0, 1.0, 16.0])
    monkeypatch.setattr(lcb_runtime_proxy, "ProcessInspector", _Inspector)
    monkeypatch.setattr(lcb_runtime_proxy.subprocess, "Popen", lambda *_args, **_kwargs: child)
    monkeypatch.setattr(lcb_runtime_proxy.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(lcb_runtime_proxy.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(lcb_runtime_proxy.time, "sleep", lambda *_args: None)
    monkeypatch.setenv(lcb_runtime_proxy.SUPERVISOR_METADATA_ENV, str(metadata_path))
    monkeypatch.setenv(lcb_runtime_proxy.SUPERVISOR_CONTRACT_ENV, LCB_RUNTIME_CONTRACT)
    monkeypatch.setenv(lcb_runtime_proxy.SUPERVISOR_RUNTIME_ENV, metadata.instance_id)
    monkeypatch.setenv(lcb_runtime_proxy.SUPERVISOR_EPOCH_ENV, str(metadata.runtime_epoch))
    monkeypatch.setenv(lcb_runtime_proxy.SUPERVISOR_TOKEN_ENV, token)

    result = lcb_runtime_proxy.run(metadata_path, ["node", "bridge.js"])
    stored = CodexRuntimeMetadata.model_validate_json(
        metadata_path.read_text(encoding="utf-8")
    )
    return result, child, stored


def test_proxy_startup_timeout_stops_only_verified_lcb_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, child, stored = _run_proxy_startup_timeout(
        tmp_path,
        monkeypatch,
        replace_child_identity=False,
    )

    assert result == 7
    assert child.terminated is True
    assert stored.failure_code == "SUPERVISOR_CODEX_RUNTIME_FAILED"
    assert stored.status == "DEGRADED"


def test_proxy_startup_timeout_refuses_pid_reused_child(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, child, stored = _run_proxy_startup_timeout(
        tmp_path,
        monkeypatch,
        replace_child_identity=True,
    )

    assert result == 7
    assert child.terminated is False
    assert stored.failure_code == "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"
    assert stored.status == "DEGRADED"


def test_proxy_contract_mismatch_fails_before_lcb_spawn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager = SupervisorCodexRuntimeManager(tmp_path)
    manager.prepare({"CODEX_HOME": str(tmp_path / "empty-home")})
    environment = manager.environment({})
    environment[lcb_runtime_proxy.SUPERVISOR_CONTRACT_ENV] = "unsupported-contract"
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(
        lcb_runtime_proxy.subprocess,
        "Popen",
        lambda *_args, **_kwargs: pytest.fail("LCB must not spawn after contract mismatch"),
    )

    result = lcb_runtime_proxy.run(manager.metadata_path, ["node", "bridge.js"])
    stored = CodexRuntimeMetadata.model_validate_json(
        manager.metadata_path.read_text(encoding="utf-8")
    )

    assert result == 4
    assert stored.ownership == CodexProcessOwnership.UNKNOWN
    assert stored.failure_code == "CODEX_RUNTIME_OWNERSHIP_UNKNOWN"


@pytest.mark.parametrize(
    "ownership",
    [CodexProcessOwnership.DESKTOP_EXTERNAL, CodexProcessOwnership.UNKNOWN],
)
def test_non_supervisor_process_cannot_stop_or_restart(
    tmp_path: Path,
    ownership: CodexProcessOwnership,
) -> None:
    launched: list[_DummyProcess] = []
    manager = ProcessManager(
        tmp_path / "runtime",
        tmp_path / "logs",
        launcher=lambda *_args, **_kwargs: launched.append(_DummyProcess(700)) or launched[-1],
    )
    process = _DummyProcess(600)
    manager._processes["codex"] = ProcessState(
        name="codex",
        status="RUNNING",
        pid=process.pid,
        ownership=ownership,
        _process=process,
    )

    stopped = manager.stop("codex")
    restarted = manager.restart(ManagedProcessSpec(name="codex", command=["codex"]))

    assert stopped.status == "UNKNOWN"
    assert restarted.status == "UNKNOWN"
    assert process.terminated is False
    assert launched == []


def test_supervisor_owned_process_may_stop(tmp_path: Path) -> None:
    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    process = _DummyProcess(601)
    manager._processes["codex"] = ProcessState(
        name="codex",
        status="RUNNING",
        pid=process.pid,
        ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        _process=process,
    )

    stopped = manager.stop("codex")

    assert stopped.status == "STOPPED"
    assert process.terminated is True


def test_runtime_instance_and_epoch_are_persisted_with_thread_identity() -> None:
    memory = MemoryService()
    try:
        _bind_verified_runtime(memory, "AFFINITY")
        runtime = get_codex_runtime(memory.store, "AFFINITY")
        task = memory.get_task("AFFINITY")

        assert runtime is not None
        assert runtime.runtime_instance_id == "csb-codex-one"
        assert runtime.runtime_epoch == 1
        assert runtime.affinity_verified is True
        assert task.agent_runtime_instance_id == runtime.runtime_instance_id
        assert task.agent_runtime_epoch == runtime.runtime_epoch
    finally:
        memory.close()


def test_runtime_replacement_invalidates_old_turn_and_pending_interaction() -> None:
    memory = MemoryService()
    try:
        old_handle = _bind_verified_runtime(memory, "EPOCH")
        replacement = close_runtime_circuit_after_recovery(
            memory.store,
            "EPOCH",
            runtime_instance_id="csb-codex-two",
            runtime_epoch=2,
            ownership="SUPERVISOR_MANAGED",
            isolation_verified=True,
            runtime_status="READY",
        )

        assert replacement.thread_id is None
        assert replacement.turn_id is None
        with pytest.raises(CodexRuntimeAffinityError):
            assert_runtime_affinity(
                replacement,
                instance_id=old_handle.runtime_instance_id,
                runtime_epoch=old_handle.runtime_epoch,
            )

        backend = _FakeBackend()
        manager = _FakeRuntimeManager(_verified_metadata(Path("C:/runtime"), instance_id="csb-codex-two", epoch=2))
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=manager,  # type: ignore[arg-type]
        )
        session._backend = backend
        current_handle = old_handle.model_copy(
            update={
                "runtime_instance_id": "csb-codex-two",
                "runtime_epoch": 2,
            }
        )
        stale = PendingInteraction(
            interaction_id="approval-1",
            kind="command_approval",
            runtime_instance_id="csb-codex-one",
            runtime_epoch=1,
        )

        with pytest.raises(CodexRuntimeAffinityError, match="pending interaction is stale"):
            asyncio.run(session.respond_interaction(current_handle, stale, {"decision": "accept"}))
        assert backend.respond_calls == 0
    finally:
        memory.close()


def test_plan_without_semantic_progress_opens_circuit_and_blocks_retries(tmp_path: Path) -> None:
    memory = MemoryService()
    try:
        handle = _bind_verified_runtime(memory, "STALLED")
        first_at = datetime.now(timezone.utc) + timedelta(seconds=1)
        first = AgentSnapshot(
            status="planning",
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            runtime_instance_id=handle.runtime_instance_id,
            runtime_epoch=handle.runtime_epoch,
            runtime_ownership="SUPERVISOR_MANAGED",
            isolation_verified=True,
            raw_event_count=10,
        )
        second = first.model_copy(update={"raw_event_count": 75})

        record_runtime_observation(memory.store, "STALLED", first, observed_at=first_at)
        stalled = record_runtime_observation(
            memory.store,
            "STALLED",
            second,
            observed_at=first_at + timedelta(minutes=3),
        )

        assert stalled.remote_status == "CODEX_TURN_STALLED"
        assert stalled.circuit_state == "OPEN"
        assert stalled.circuit_reason == "CODEX_TURN_STALLED"
        assert stalled.next_action == "USER_ACTION_REQUIRED"

        backend = _FakeBackend()
        manager = _FakeRuntimeManager(
            _verified_metadata(tmp_path, instance_id="csb-codex-one")
        )
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=manager,  # type: ignore[arg-type]
        )
        session._backend = backend

        with pytest.raises(CodexRuntimeCircuitOpenError):
            asyncio.run(
                session.start_plan(
                    task_id="STALLED",
                    context_pack="context",
                    workspace=WorkspaceState(
                        workspace_id="ws",
                        repository="C:/repo",
                        root="C:/repo",
                    ),
                )
            )
        assert backend.start_calls == 0

        interrupted = asyncio.run(session.interrupt(handle))
        assert interrupted.status == "interrupted"
        with pytest.raises(CodexRuntimeCircuitOpenError):
            asyncio.run(session.interrupt(handle))
        assert backend.interrupt_calls == 1
    finally:
        memory.close()


def test_open_circuit_blocks_execution_and_resume(tmp_path: Path) -> None:
    memory = MemoryService()
    backend = _FakeBackend()
    try:
        handle = _bind_verified_runtime(memory, "CIRCUIT-CONTROL")
        open_runtime_circuit(
            memory.store,
            "CIRCUIT-CONTROL",
            reason="CODEX_TURN_STALLED",
            remote_status="CODEX_TURN_STALLED",
            recovery_required=False,
        )
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=_FakeRuntimeManager(
                _verified_metadata(tmp_path, instance_id="csb-codex-one")
            ),  # type: ignore[arg-type]
        )
        session._backend = backend
        workspace = WorkspaceState(
            workspace_id="ws",
            repository="C:/repo",
            root="C:/repo",
        )
        lease = WriterLeaseToken(
            task_id="CIRCUIT-CONTROL",
            writer=ActiveWriter.CODEX,
            writer_epoch=1,
            task_revision=1,
        )

        with pytest.raises(CodexRuntimeCircuitOpenError):
            asyncio.run(
                session.start_execution(
                    task_id="CIRCUIT-CONTROL",
                    context_pack="context",
                    approved_plan="approved",
                    workspace=workspace,
                    lease=lease,
                )
            )
        with pytest.raises(CodexRuntimeCircuitOpenError):
            asyncio.run(session.resume(handle))

        assert backend.execution_calls == 0
        assert backend.resume_calls == 0
    finally:
        memory.close()


def test_lcb_restart_never_resumes_thread_from_another_runtime_instance(
    tmp_path: Path,
) -> None:
    memory = MemoryService()
    backend = _FakeBackend()
    try:
        _bind_verified_runtime(memory, "RECONNECT-AFFINITY")
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=_FakeRuntimeManager(
                _verified_metadata(
                    tmp_path,
                    instance_id="csb-codex-new",
                    epoch=2,
                )
            ),  # type: ignore[arg-type]
        )

        outcomes = asyncio.run(session.start())

        assert outcomes[0].task_id == "RECONNECT-AFFINITY"
        assert outcomes[0].status == "RECONCILIATION_REQUIRED"
        assert "instance/epoch" in outcomes[0].detail
        assert backend.resume_calls == 0
        asyncio.run(session.shutdown())
    finally:
        memory.close()


def test_unexpected_runtime_result_opens_reconciliation_circuit() -> None:
    memory = MemoryService()
    try:
        handle = _bind_verified_runtime(memory, "UNEXPECTED")
        snapshot = AgentSnapshot(
            status="UNKNOWN",
            reconciliation_required=True,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            runtime_instance_id=handle.runtime_instance_id,
            runtime_epoch=handle.runtime_epoch,
            runtime_ownership="SUPERVISOR_MANAGED",
            isolation_verified=True,
        )

        runtime = record_runtime_observation(memory.store, "UNEXPECTED", snapshot)

        assert runtime.circuit_state == "RECOVERY_REQUIRED"
        assert runtime.circuit_reason == "CODEX_RUNTIME_RECONCILIATION_REQUIRED"
        assert runtime.next_action == "RUNTIME_RECOVERY_REQUIRED"
    finally:
        memory.close()


def test_lcb_without_isolation_capability_uses_control_plane_only_for_unbound_task() -> None:
    health = {
        "devspace": BackendHealth(
            capability="devspace",
            status=BackendHealthStatus.READY,
            user_message="ready",
        ),
        "local_codex_bridge": BackendHealth(
            capability="local_codex_bridge",
            status=BackendHealthStatus.READY,
            user_message="ready",
            capabilities={"supports_isolated_runtime": False},
        ),
        "kandev": BackendHealth(
            capability="kandev",
            status=BackendHealthStatus.READY,
            user_message="ready",
        ),
        "control_plane": BackendHealth(
            capability="control_plane",
            status=BackendHealthStatus.READY,
            user_message="ready",
        ),
        "codex": BackendHealth(
            capability="codex",
            status=BackendHealthStatus.READY,
            user_message="ready",
        ),
    }

    selection = RuntimeResolver(health).resolve()

    assert selection.profile == "existing"
    assert selection.agent_backend == "control_plane"
    assert selection.reason == "Profile B is not ready and Profile A capabilities are ready"


def test_runtime_metadata_public_view_never_contains_pid_or_ownership_token(tmp_path: Path) -> None:
    metadata = _verified_metadata(tmp_path)
    public = metadata.public_status()

    assert "pid" not in json.dumps(public).lower()
    assert "ownership_token" not in json.dumps(public).lower()
    assert public["ownership"] == "SUPERVISOR_MANAGED"
    assert public["isolation_verified"] is True


def test_process_identity_requires_creation_command_and_parent_identity(tmp_path: Path) -> None:
    metadata = _verified_metadata(tmp_path)
    manager = SupervisorCodexRuntimeManager(tmp_path)
    assert metadata.isolation_verified is True

    reused_lcb = metadata.model_copy(
        update={
            "lcb_process": metadata.lcb_process.model_copy(  # type: ignore[union-attr]
                update={"creation_time": "different-creation-time"}
            )
        }
    )
    assert manager.verify_metadata(reused_lcb).isolation_verified is False

    class _ChangedCommandInspector:
        def snapshot(self) -> list[ProcessObservation]:
            return [
                metadata.proxy_process.model_copy(  # type: ignore[union-attr]
                    update={"command_line_fingerprint": "different-command"}
                ),
                metadata.lcb_process,  # type: ignore[list-item]
                metadata.app_server_process,  # type: ignore[list-item]
            ]

    runtime_directory = Path(metadata.runtime_directory)
    runtime_directory.mkdir(parents=True)
    (runtime_directory / "runtime.json").write_text(
        json.dumps(metadata.model_dump(mode="json")),
        encoding="utf-8",
    )
    destructive = SupervisorCodexRuntimeManager(
        tmp_path,
        inspector=_ChangedCommandInspector(),  # type: ignore[arg-type]
    )
    destructive.metadata = metadata
    destructive._token = "owned-token"

    with pytest.raises(RuntimeOwnershipError, match="process identity changed"):
        destructive.assert_destructive_lifecycle_allowed()

    assert (
        runtime_process_chain_failure(
            metadata,
            [
                metadata.proxy_process,  # type: ignore[list-item]
                metadata.lcb_process,  # type: ignore[list-item]
            ],
        )
        == "Codex app-server process is not running"
    )


def test_interrupt_unknown_outcome_opens_circuit_and_never_retries(tmp_path: Path) -> None:
    class _UnknownInterruptBackend(_FakeBackend):
        async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
            self.interrupt_calls += 1
            return AgentSnapshot(
                status="UNKNOWN",
                reconciliation_required=True,
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
                blockers=["codex_interrupt acknowledgement timed out"],
            )

    memory = MemoryService()
    try:
        handle = _bind_verified_runtime(memory, "INTERRUPT-TIMEOUT")
        backend = _UnknownInterruptBackend()
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=_FakeRuntimeManager(
                _verified_metadata(tmp_path, instance_id="csb-codex-one")
            ),  # type: ignore[arg-type]
        )
        session._backend = backend

        snapshot = asyncio.run(session.interrupt(handle))
        runtime = get_codex_runtime(memory.store, "INTERRUPT-TIMEOUT")

        assert snapshot.status == "UNKNOWN"
        assert runtime is not None
        assert runtime.circuit_state == "RECOVERY_REQUIRED"
        assert runtime.circuit_reason == "TURN_INTERRUPT_TIMEOUT"
        assert runtime.remote_status == "APP_SERVER_UNRESPONSIVE"
        with pytest.raises(CodexRuntimeCircuitOpenError):
            asyncio.run(session.interrupt(handle))
        assert backend.interrupt_calls == 1
    finally:
        memory.close()


def test_lcb_session_loss_on_pending_request_opens_disconnect_circuit(tmp_path: Path) -> None:
    class _DisconnectedBackend(_FakeBackend):
        async def list_pending_interactions(
            self,
            _handle: PlanHandle,
        ) -> list[PendingInteraction]:
            raise BrokenPipeError("closed stdio")

    memory = MemoryService()
    try:
        handle = _bind_verified_runtime(memory, "SESSION-LOST")
        backend = _DisconnectedBackend()
        runtime_manager = _FakeRuntimeManager(
            _verified_metadata(tmp_path, instance_id="csb-codex-one")
        )
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=runtime_manager,  # type: ignore[arg-type]
        )
        session._backend = backend

        with pytest.raises(BrokenPipeError):
            asyncio.run(session.list_pending_interactions(handle))

        runtime = get_codex_runtime(memory.store, "SESSION-LOST")
        assert runtime is not None
        assert runtime.circuit_state == "RECOVERY_REQUIRED"
        assert runtime.circuit_reason == "CODEX_APP_SERVER_DISCONNECTED"
        assert runtime_manager.degraded == ["CODEX_APP_SERVER_DISCONNECTED:BrokenPipeError"]
    finally:
        memory.close()


def test_profile_b_health_probe_reuses_one_persistent_runtime_session(tmp_path: Path) -> None:
    memory = MemoryService()
    created: list[_FakeBackend] = []

    def factory() -> _FakeBackend:
        backend = _FakeBackend()
        created.append(backend)
        return backend

    session = AgentSessionManager(
        memory,
        factory,
        runtime_manager=_FakeRuntimeManager(_verified_metadata(tmp_path)),  # type: ignore[arg-type]
    )

    async def scenario() -> None:
        first = await session.probe_health()
        second = await session.probe_health()
        assert first.status == BackendHealthStatus.READY
        assert second.status == BackendHealthStatus.READY
        assert session.session_count == 1
        assert len(created) == 1
        await session.shutdown()

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_first_plan_disconnect_degrades_runtime_and_blocks_retry(tmp_path: Path) -> None:
    class _DisconnectedStartBackend(_FakeBackend):
        async def start_plan(self, **_kwargs: Any) -> PlanHandle:
            self.start_calls += 1
            raise BrokenPipeError("closed stdio")

    memory = MemoryService()
    backend = _DisconnectedStartBackend()
    manager = _FakeRuntimeManager(_verified_metadata(tmp_path))
    try:
        memory.create_task("FIRST-DISCONNECT", "first plan", repository="C:/repo")
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=manager,  # type: ignore[arg-type]
        )
        workspace = WorkspaceState(
            workspace_id="ws",
            repository="C:/repo",
            root="C:/repo",
        )

        with pytest.raises(BrokenPipeError):
            asyncio.run(
                session.start_plan(
                    task_id="FIRST-DISCONNECT",
                    context_pack="context",
                    workspace=workspace,
                )
            )

        assert get_codex_runtime(memory.store, "FIRST-DISCONNECT") is None
        assert manager.metadata.status == "DEGRADED"
        assert manager.metadata.failure_code == "CODEX_APP_SERVER_DISCONNECTED"
        assert session.runtime_error == "CODEX_APP_SERVER_DISCONNECTED: BrokenPipeError"

        with pytest.raises(AgentSessionUnavailableError, match="CODEX_APP_SERVER_DISCONNECTED"):
            asyncio.run(
                session.start_plan(
                    task_id="FIRST-DISCONNECT",
                    context_pack="context",
                    workspace=workspace,
                )
            )
        assert backend.start_calls == 1
        asyncio.run(session.shutdown())
    finally:
        memory.close()


def test_runtime_recovery_closes_circuit_only_after_verified_ready(tmp_path: Path) -> None:
    memory = MemoryService()
    try:
        _bind_verified_runtime(memory, "RECOVERY")
        open_runtime_circuit(
            memory.store,
            "RECOVERY",
            reason="CODEX_APP_SERVER_DISCONNECTED",
            remote_status="CODEX_APP_SERVER_DISCONNECTED",
        )
        with pytest.raises(CodexRuntimeAffinityError):
            close_runtime_circuit_after_recovery(
                memory.store,
                "RECOVERY",
                runtime_instance_id="csb-codex-two",
                runtime_epoch=2,
                ownership="SUPERVISOR_MANAGED",
                isolation_verified=True,
                runtime_status="DEGRADED",
            )

        backend = _FakeBackend()
        manager = _FakeRuntimeManager(
            _verified_metadata(tmp_path, instance_id="csb-codex-one")
        )
        session = AgentSessionManager(
            memory,
            lambda: backend,
            runtime_manager=manager,  # type: ignore[arg-type]
        )
        session._backend = backend

        recovered = asyncio.run(session.recover_runtime("RECOVERY"))

        assert recovered.runtime_instance_id == "csb-codex-replacement"
        assert recovered.runtime_epoch == 2
        assert recovered.circuit_state == "CLOSED"
        assert recovered.thread_id is None
        assert recovered.turn_id is None
        assert recovered.remote_status == "not_reconstructable"
    finally:
        memory.close()


def test_desktop_and_supervisor_process_failures_are_bidirectionally_isolated(
    tmp_path: Path,
) -> None:
    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    desktop = _DummyProcess(801)
    supervisor = _DummyProcess(802)
    manager._processes["desktop"] = ProcessState(
        name="desktop",
        status="RUNNING",
        pid=desktop.pid,
        ownership=CodexProcessOwnership.DESKTOP_EXTERNAL,
        _process=desktop,
    )
    manager._processes["supervisor"] = ProcessState(
        name="supervisor",
        status="RUNNING",
        pid=supervisor.pid,
        ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        _process=supervisor,
    )

    desktop.returncode = -1
    assert manager.health("desktop").status == "CRASHED"
    assert manager.health("supervisor").status == "RUNNING"

    desktop.returncode = None
    manager._processes["desktop"].status = "RUNNING"
    manager._processes["desktop"].pid = desktop.pid
    manager._processes["desktop"]._process = desktop
    supervisor.returncode = -1
    assert manager.health("supervisor").status == "CRASHED"
    assert manager.health("desktop").status == "RUNNING"


def test_lcb_restart_never_terminates_desktop_owned_process(tmp_path: Path) -> None:
    launched: list[_DummyProcess] = []
    manager = ProcessManager(
        tmp_path / "runtime",
        tmp_path / "logs",
        launcher=lambda *_args, **_kwargs: launched.append(_DummyProcess(903)) or launched[-1],
    )
    desktop = _DummyProcess(901)
    lcb = _DummyProcess(902)
    manager._processes["desktop"] = ProcessState(
        name="desktop",
        status="RUNNING",
        pid=desktop.pid,
        ownership=CodexProcessOwnership.DESKTOP_EXTERNAL,
        _process=desktop,
    )
    manager._processes["lcb"] = ProcessState(
        name="lcb",
        status="RUNNING",
        pid=lcb.pid,
        ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        _process=lcb,
    )

    restarted = manager.restart(
        ManagedProcessSpec(
            name="lcb",
            command=["node", "dist/src/index.js"],
            ownership=CodexProcessOwnership.SUPERVISOR_MANAGED,
        )
    )

    assert restarted.status == "RUNNING"
    assert restarted.pid == 903
    assert lcb.terminated is True
    assert desktop.terminated is False
    assert manager.health("desktop").status == "RUNNING"


def test_desktop_restart_does_not_change_supervisor_runtime_identity(tmp_path: Path) -> None:
    before = _verified_metadata(tmp_path, desktop_pid=1001, app_server_pid=1002)
    after = before.model_copy(
        update={
            "desktop_processes": [
                _observation(
                    1003,
                    parent_pid=50,
                    executable="codex.exe",
                    app_server=True,
                    parent_executable="ChatGPT.exe",
                )
            ]
        }
    )
    manager = SupervisorCodexRuntimeManager(tmp_path)
    verified_after = manager.verify_metadata(after)

    assert verified_after.isolation_verified is True
    assert verified_after.instance_id == before.instance_id
    assert verified_after.runtime_epoch == before.runtime_epoch
    assert verified_after.app_server_process.pid == 1002  # type: ignore[union-attr]
    assert verified_after.desktop_processes[0].pid == 1003
    assert verified_after.advanced_status()["desktop_detection_code"] == (
        "CODEX_DESKTOP_RUNTIME_DETECTED"
    )


def test_supervisor_restart_preserves_canonical_task_memory(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    first = MemoryService(database)
    task = first.create_task(
        "RESTART-MEMORY",
        "preserve task memory",
        repository="C:/repo",
        goal="Keep canonical state across Supervisor restart",
    )
    bind_codex_runtime(
        first.store,
        task.task_id,
        task.revision,
        event_type=EventType.CODEX_STARTED,
        thread_id="thread-old",
        turn_id="turn-old",
        remote_status="planning",
        task_phase=TaskPhase.PLANNING,
        runtime_instance_id="csb-codex-old",
        runtime_epoch=1,
        runtime_ownership="SUPERVISOR_MANAGED",
        isolation_verified=True,
    )
    first.close()

    reopened = MemoryService(database)
    try:
        restored_task = reopened.get_task("RESTART-MEMORY")
        restored_runtime = get_codex_runtime(reopened.store, "RESTART-MEMORY")

        assert restored_task.current_goal == "Keep canonical state across Supervisor restart"
        assert restored_runtime is not None
        assert restored_runtime.runtime_instance_id == "csb-codex-old"
        assert restored_runtime.runtime_epoch == 1
    finally:
        reopened.close()
