from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from codex_supervisor_bridge.bootstrap import (
    AppDataPaths,
    ComponentInstaller,
    ManagedComponentRegistry,
    MemorySecretStore,
    OpenAISecureMcpTunnelConfig,
    OpenAISecureMcpTunnelController,
    OpenAISecureMcpTunnelValidator,
    ProcessManager,
    RemoteAccessFailure,
    RemoteAccessMode,
)
from codex_supervisor_bridge.bootstrap.configuration import AppConfig, ConfigStore
from codex_supervisor_bridge.bootstrap.process import ProcessState


def test_openai_remote_config_is_loopback_and_has_no_public_url() -> None:
    config = OpenAISecureMcpTunnelConfig(
        tunnel_id="tunnel_gate123",
        runtime_secret_ref="openai-runtime",
    )

    assert config.provider == RemoteAccessMode.OPENAI_SECURE_MCP_TUNNEL
    assert not hasattr(config, "public_url")
    assert OpenAISecureMcpTunnelValidator.validate(config) == []

    with pytest.raises(ValueError):
        OpenAISecureMcpTunnelConfig(
            tunnel_id="tunnel_gate123",
            runtime_secret_ref="openai-runtime",
            local_mcp_url="http://0.0.0.0:8765/mcp",
        )


def test_remote_config_round_trip_preserves_typed_tunnel_fields(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )
    config = AppConfig.safe_defaults(paths)
    config.advanced.remote_access = OpenAISecureMcpTunnelConfig(
        tunnel_id="tunnel_gate123",
        runtime_secret_ref="runtime",
    )
    store = ConfigStore(paths=paths)
    store.save(config)

    loaded = store.load().config.advanced.remote_access

    assert isinstance(loaded, OpenAISecureMcpTunnelConfig)
    assert loaded.tunnel_id == "tunnel_gate123"
    assert loaded.runtime_secret_ref == "runtime"


def test_openai_remote_rejects_non_pinned_version() -> None:
    with pytest.raises(ValueError):
        OpenAISecureMcpTunnelConfig(
            tunnel_id="tunnel_gate123",
            runtime_secret_ref="openai-runtime",
            client_version="0.0.14",
        )


def test_tunnel_client_manifest_is_pinned_to_official_release_and_sidecar() -> None:
    manifest = ManagedComponentRegistry().manifest("openai-tunnel-client")

    assert manifest.version == "0.0.13"
    assert manifest.source.startswith("https://github.com/openai/tunnel-client/releases/")
    assert manifest.checksum_source.endswith("/SHA256SUMS.txt")
    assert manifest.checksum_entry.endswith("windows-amd64.zip")
    assert manifest.entrypoint == "tunnel-client.exe"
    assert "latest" not in manifest.source.lower()


def test_installer_verifies_official_checksum_sidecar_before_promote(tmp_path: Path) -> None:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("tunnel-client.exe", "binary")
    payload = stream.getvalue()
    import hashlib

    digest = hashlib.sha256(payload).hexdigest()

    def downloader(url: str, destination: Path) -> Path:
        if url.endswith("SHA256SUMS.txt"):
            destination.write_text(f"{digest}  artifact.zip\n", encoding="utf-8")
        else:
            destination.write_bytes(payload)
        return destination

    manifest = ManagedComponentRegistry().manifest("openai-tunnel-client").model_copy(
        update={
            "source": "https://example.invalid/artifact.zip",
            "checksum_source": "https://example.invalid/SHA256SUMS.txt",
            "checksum_entry": "artifact.zip",
            "checksum_sha256": digest,
            "version_args": [],
            "version_contains": None,
        }
    )
    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        trusted_manifests={manifest.name: manifest},
        max_retries=1,
    )

    result = installer.install(manifest)

    assert result.status == "INSTALLED"
    assert result.installed_path is not None
    assert (result.installed_path / "tunnel-client.exe").is_file()


def test_tunnel_controller_keeps_runtime_key_out_of_command_and_state(tmp_path: Path) -> None:
    secret = "SUPER_SECRET_TUNNEL_RUNTIME_KEY_123456"
    secrets = MemorySecretStore()
    secrets.set("runtime", secret)
    observed: dict[str, object] = {}

    class FakeProcess:
        pid = 321
        returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            del timeout
            return 0

        def kill(self):
            self.returncode = -9

    def launch(command, **kwargs):
        observed["command"] = list(command)
        observed["env"] = kwargs["env"]
        return FakeProcess()

    manager = ProcessManager(
        tmp_path / "runtime",
        tmp_path / "logs",
        launcher=launch,
    )
    controller = OpenAISecureMcpTunnelController(
        process_manager=manager,
        secret_store=secrets,
        executable="tunnel-client",
        runtime_dir=tmp_path / "runtime",
    )
    controller._probe_ready = lambda path: True
    config = OpenAISecureMcpTunnelConfig(
        tunnel_id="tunnel_gate123",
        runtime_secret_ref="runtime",
    )
    health = controller.start(config)
    rendered = json.dumps(
        {"command": observed["command"], "processes": json.loads((tmp_path / "runtime" / "processes.json").read_text())},
        ensure_ascii=True,
    )

    assert health.runtime_key_present is True
    assert secret not in rendered
    assert secret not in json.dumps(config.model_dump(mode="json"), ensure_ascii=True)
    assert "env:CODEX_SUPERVISOR_TUNNEL_RUNTIME_KEY" in observed["command"]
    assert observed["env"]["CODEX_SUPERVISOR_TUNNEL_RUNTIME_KEY"] == secret


def test_tunnel_controller_requires_supervisor_ready_and_runtime_key(tmp_path: Path) -> None:
    config = OpenAISecureMcpTunnelConfig(
        tunnel_id="tunnel_gate123",
        runtime_secret_ref="runtime",
    )
    controller = OpenAISecureMcpTunnelController(
        process_manager=ProcessManager(tmp_path / "runtime", tmp_path / "logs"),
        secret_store=MemorySecretStore(),
        executable="tunnel-client",
        runtime_dir=tmp_path / "runtime",
    )

    blocked = controller.start(config, supervisor_ready=False)
    missing_key = controller.start(config, supervisor_ready=True)

    assert blocked.state == RemoteAccessFailure.LOCAL_MCP_UNREACHABLE.value
    assert missing_key.state == RemoteAccessFailure.TUNNEL_RUNTIME_KEY_MISSING.value


def test_tunnel_controller_reports_missing_client_without_leaking_key(tmp_path: Path) -> None:
    secret = "SUPER_SECRET_TUNNEL_RUNTIME_KEY_123456"
    secrets = MemorySecretStore()
    secrets.set("runtime", secret)
    controller = OpenAISecureMcpTunnelController(
        process_manager=ProcessManager(tmp_path / "runtime", tmp_path / "logs"),
        secret_store=secrets,
        executable=str(tmp_path / "missing" / "tunnel-client.exe"),
        runtime_dir=tmp_path / "runtime",
    )

    health = controller.start(
        OpenAISecureMcpTunnelConfig(
            tunnel_id="tunnel_gate123",
            runtime_secret_ref="runtime",
        )
    )

    assert health.state == RemoteAccessFailure.TUNNEL_CLIENT_MISSING.value
    assert health.runtime_key_present is True
    assert secret not in health.model_dump_json()


def test_tunnel_controller_reports_not_ready_before_first_start(tmp_path: Path) -> None:
    controller = OpenAISecureMcpTunnelController(
        process_manager=ProcessManager(tmp_path / "runtime", tmp_path / "logs"),
        secret_store=MemorySecretStore(),
        executable="tunnel-client",
        runtime_dir=tmp_path / "runtime",
    )

    health = controller.health()

    assert health.state == RemoteAccessFailure.TUNNEL_NOT_READY.value
    assert health.process_running is False


def test_tunnel_controller_distinguishes_health_from_ready(tmp_path: Path, monkeypatch) -> None:
    secrets = MemorySecretStore()
    secrets.set("runtime", "opaque")
    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    controller = OpenAISecureMcpTunnelController(
        process_manager=manager,
        secret_store=secrets,
        executable="tunnel-client",
        runtime_dir=tmp_path / "runtime",
    )
    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.remote._probe_endpoint",
        lambda url, path: path == "/healthz",
    )
    monkeypatch.setattr(
        manager,
        "health",
        lambda name: ProcessState(name, "RUNNING", pid=123),
    )
    controller._health = controller._health.model_copy(
        update={"health_url": "http://127.0.0.1:1234"}
    )
    health = controller.health()

    assert health.healthy is True
    assert health.ready is False
    assert health.state == RemoteAccessFailure.TUNNEL_NOT_READY.value


def test_pid_reuse_can_be_cleared_without_touching_live_process(tmp_path: Path, monkeypatch) -> None:
    manager = ProcessManager(tmp_path / "runtime", tmp_path / "logs")
    (tmp_path / "runtime" / "processes.json").write_text(
        json.dumps(
            {
                "supervisor": {
                    "status": "RUNNING",
                    "pid": 28268,
                    "process_identity": {
                        "executable": "C:/bridge/supervisor.exe",
                        "started_at": 1,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.process._pid_exists",
        lambda pid: pid == 28268,
    )
    monkeypatch.setattr(
        "codex_supervisor_bridge.bootstrap.process._process_identity",
        lambda pid: {"executable": "C:/other/claude-code-mcp.exe", "started_at": 2},
    )

    state = manager.health("supervisor")
    observed_status = state.status
    observed_identity_status = state.identity_status
    cleared = manager.repair_stale("supervisor")

    assert observed_status == "UNKNOWN"
    assert observed_identity_status == "PID_REUSED"
    assert cleared.status == "STOPPED"
    assert cleared.identity_status == "CLEARED_PID_REUSE"


def test_local_mcp_url_must_not_expose_private_components() -> None:
    with pytest.raises(ValueError):
        OpenAISecureMcpTunnelConfig(
            tunnel_id="tunnel_gate123",
            runtime_secret_ref="runtime",
            local_mcp_url="http://192.168.1.10:8765/mcp",
        )
