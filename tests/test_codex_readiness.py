from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.bootstrap import (
    CodexAuthMode,
    CodexConfigInspector,
    CodexReadinessDetector,
)
from codex_supervisor_bridge.bootstrap.configuration import ConfigStore
from codex_supervisor_bridge.bootstrap.doctor import Doctor
from codex_supervisor_bridge.bootstrap.models import HealthStatus
from codex_supervisor_bridge.bootstrap.paths import AppDataPaths
from codex_supervisor_bridge.supervisor.runtime import RuntimeComposition

SENTINEL = "SUPER_SECRET_PROVIDER_KEY_123456"


class FakeCodexRunner:
    def __init__(
        self,
        *,
        version: str = "codex 1.2.3\n",
        smoke_stdout: str = "",
        smoke_stderr: str = "",
        smoke_returncode: int = 0,
        marker: bool = True,
        timeout: bool = False,
    ) -> None:
        self.version = version
        self.smoke_stdout = smoke_stdout
        self.smoke_stderr = smoke_stderr
        self.smoke_returncode = smoke_returncode
        self.marker = marker
        self.timeout = timeout
        self.commands: list[list[str]] = []

    def __call__(
        self,
        command: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        del kwargs
        self.commands.append(list(command))
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, self.version, "")
        if self.timeout:
            raise subprocess.TimeoutExpired(cmd=command, timeout=30)
        if self.marker and self.smoke_returncode == 0:
            try:
                index = command.index("--output-last-message")
                Path(command[index + 1]).write_text(
                    "CODEX_RUNTIME_READY",
                    encoding="utf-8",
                )
            except (OSError, ValueError):
                pass
        return subprocess.CompletedProcess(
            command,
            self.smoke_returncode,
            self.smoke_stdout,
            self.smoke_stderr,
        )


def write_custom_config(
    path: Path,
    *,
    provider: str = "thirdparty",
    env_key: str | None = "THIRD_PARTY_API_KEY",
    base_url: str = "https://provider.example/v1",
    requires_openai_auth: bool = False,
) -> None:
    lines = [
        'model = "gpt-5.6-luna"',
        f'model_provider = "{provider}"',
        f"[model_providers.{provider}]",
        f'name = "{provider.title()}"',
        f'base_url = "{base_url}"',
        'wire_api = "responses"',
        f"requires_openai_auth = {str(requires_openai_auth).lower()}",
    ]
    if env_key is not None:
        lines.append(f'env_key = "{env_key}"')
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_config_inspector_reports_provider_env_key_without_value(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_custom_config(config)

    inspection = CodexConfigInspector(
        config_path=config,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).inspect()

    assert inspection.config_health == "ready"
    assert inspection.provider_type == "thirdparty"
    assert inspection.auth_mode == CodexAuthMode.PROVIDER_ENV_KEY
    assert inspection.credential_source_type == "environment"
    assert inspection.credential_reference == "THIRD_PARTY_API_KEY"
    assert inspection.credential_present is True
    assert inspection.base_url_masked == "https://provider.example"
    rendered = json.dumps(inspection.model_dump(mode="json"))
    assert SENTINEL not in rendered


def test_config_inspector_distinguishes_auth_mode_categories(tmp_path: Path) -> None:
    chatgpt = tmp_path / "chatgpt.toml"
    chatgpt.write_text(
        'model_provider = "openai"\n'
        "[model_providers.openai]\n"
        "requires_openai_auth = true\n",
        encoding="utf-8",
    )
    chatgpt_inspection = CodexConfigInspector(config_path=chatgpt).inspect()
    assert chatgpt_inspection.auth_mode == CodexAuthMode.CHATGPT_ACCOUNT
    assert chatgpt_inspection.credential_source_type == "chatgpt_account"
    assert chatgpt_inspection.requires_openai_account is True

    api_key = tmp_path / "api-key.toml"
    write_custom_config(api_key, env_key="OPENAI_API_KEY")
    api_key_inspection = CodexConfigInspector(
        config_path=api_key,
        environ={"OPENAI_API_KEY": "sk-test-not-exported"},
    ).inspect()
    assert api_key_inspection.auth_mode == CodexAuthMode.OPENAI_API_KEY

    local = tmp_path / "local.toml"
    write_custom_config(
        local,
        provider="local",
        env_key=None,
        base_url="http://127.0.0.1:11434/v1",
    )
    local_inspection = CodexConfigInspector(config_path=local).inspect()
    assert local_inspection.auth_mode == CodexAuthMode.LOCAL_NO_AUTH
    assert local_inspection.credential_source_type == "none"

    custom = tmp_path / "custom.toml"
    write_custom_config(custom, provider="custom", env_key=None)
    custom_inspection = CodexConfigInspector(config_path=custom).inspect()
    assert custom_inspection.auth_mode == CodexAuthMode.CUSTOM_PROVIDER


def test_custom_provider_runtime_success_is_ready_without_login(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_custom_config(config)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".git").mkdir()
    runner = FakeCodexRunner()
    before = config.read_bytes()

    readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=runner,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).probe(executable="codex", workspace=workspace, config_path=config)

    assert readiness.status.value == "READY"
    assert readiness.runtime_ready is True
    assert readiness.auth_mode == CodexAuthMode.PROVIDER_ENV_KEY
    assert readiness.config is not None
    assert readiness.config.credential_reference == "THIRD_PARTY_API_KEY"
    assert readiness.runtime_probe is not None
    assert readiness.runtime_probe.category == "SUCCESS"
    assert config.read_bytes() == before
    assert SENTINEL not in json.dumps(readiness.model_dump(mode="json"))

    smoke = next(command for command in runner.commands if "exec" in command)
    assert "--sandbox" in smoke
    assert smoke[smoke.index("--sandbox") + 1] == "read-only"
    assert "--json" in smoke
    assert "--ephemeral" in smoke
    assert "--cd" in smoke
    assert "--skip-git-repo-check" not in smoke
    rendered_commands = " ".join(" ".join(item) for item in runner.commands).lower()
    assert "login" not in rendered_commands
    assert "logout" not in rendered_commands


def test_env_key_missing_degrades_without_runtime_smoke(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_custom_config(config)
    runner = FakeCodexRunner()

    readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=runner,
        environ={},
    ).probe(executable="codex", config_path=config)

    assert readiness.status.value == "DEGRADED"
    assert readiness.runtime_ready is False
    assert readiness.requires_user_action is True
    assert readiness.runtime_probe is None
    assert readiness.technical_detail == "missing environment variable: THIRD_PARTY_API_KEY"
    assert readiness.user_message == "Codex 当前凭据不可用。"
    assert len(runner.commands) == 1
    assert "exec" not in runner.commands[0]


def test_invalid_codex_config_fails_closed_without_modification(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text("model_provider = [unterminated\n", encoding="utf-8")
    before = config.read_bytes()
    runner = FakeCodexRunner()

    readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=runner,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).probe(executable="codex", config_path=config)

    assert readiness.status.value == "DEGRADED"
    assert readiness.runtime_probe is None
    assert readiness.repairable is True
    assert readiness.config is not None
    assert readiness.config.config_health == "invalid"
    assert config.read_bytes() == before
    assert len(runner.commands) == 1


def test_provider_auth_failure_is_classified_without_secret_exposure(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_custom_config(config)
    runner = FakeCodexRunner(
        marker=False,
        smoke_returncode=1,
        smoke_stderr=f"HTTP 401 invalid api key {SENTINEL}\n",
    )

    readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=runner,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).probe(executable="codex", config_path=config)

    assert readiness.status.value == "DEGRADED"
    assert readiness.requires_user_action is True
    assert readiness.runtime_probe is not None
    assert readiness.runtime_probe.category == "PROVIDER_AUTH_FAILED"
    assert readiness.user_message == "Codex 当前凭据无法使用。"
    rendered = json.dumps(readiness.model_dump(mode="json"))
    assert SENTINEL not in rendered
    assert "invalid api key" not in rendered.lower()


def test_provider_timeout_and_model_unavailable_are_separate_failures(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    write_custom_config(config)
    timeout_runner = FakeCodexRunner(timeout=True)
    timeout_readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=timeout_runner,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).probe(executable="codex", config_path=config)
    assert timeout_readiness.status.value == "DEGRADED"
    assert timeout_readiness.runtime_probe is not None
    assert timeout_readiness.runtime_probe.category == "PROVIDER_TIMEOUT"

    model_runner = FakeCodexRunner(
        marker=False,
        smoke_returncode=1,
        smoke_stderr="model_not_found\n",
    )
    model_readiness = CodexReadinessDetector(
        finder=lambda name: "C:/tools/codex.exe",
        runner=model_runner,
        environ={"THIRD_PARTY_API_KEY": SENTINEL},
    ).probe(executable="codex", config_path=config)
    assert model_readiness.status.value == "DEGRADED"
    assert model_readiness.runtime_probe is not None
    assert model_readiness.runtime_probe.category == "MODEL_UNAVAILABLE"
    assert model_readiness.requires_user_action is True


def test_secret_sentinel_is_absent_from_full_doctor_output(
    tmp_path: Path,
    monkeypatch,
) -> None:
    app_root = tmp_path / "app"
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    config = codex_home / "config.toml"
    write_custom_config(config)
    monkeypatch.setenv("CODEX_SUPERVISOR_DATA_DIR", str(app_root))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("THIRD_PARTY_API_KEY", SENTINEL)

    paths = AppDataPaths.from_environment()
    runner = FakeCodexRunner()
    doctor = Doctor(
        paths=paths,
        config_store=ConfigStore(paths=paths),
        executable_finder=lambda name: "C:/tools/codex.exe" if name == "codex" else None,
        command_runner=runner,
    )
    status = doctor.run()
    codex = status.component("Codex")

    assert codex is not None
    assert codex.status.value == "READY"
    assert codex.advanced["auth_mode"] == "provider_env_key"
    assert codex.advanced["config"]["credential_reference"] == "THIRD_PARTY_API_KEY"
    rendered = json.dumps(status.model_dump(mode="json"), ensure_ascii=True)
    assert SENTINEL not in rendered
    assert "SUPER_SECRET_PROVIDER_KEY" not in rendered


def test_doctor_preserves_runtime_smoke_timeout_budget(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    write_custom_config(codex_home / "config.toml")
    app_root = tmp_path / "app"
    project = tmp_path / "project"
    (project / ".git").mkdir(parents=True)
    commands: list[tuple[list[str], dict[str, object]]] = []

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append((list(command), dict(kwargs)))
        if "--version" in command:
            return subprocess.CompletedProcess(command, 0, "codex 1.2.3\n", "")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text("CODEX_RUNTIME_READY", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, "", "")

    doctor = Doctor(
        paths=AppDataPaths.from_environment(
            environ={
                "CODEX_SUPERVISOR_DATA_DIR": str(app_root),
                "CODEX_HOME": str(codex_home),
                "THIRD_PARTY_API_KEY": "present-but-never-rendered",
            },
            system="Windows",
        ),
        executable_finder=lambda name: "C:/tools/codex.exe" if name == "codex" else None,
        command_runner=runner,
    )
    status = doctor.run()
    codex = status.component("Codex")

    assert codex is not None
    assert codex.status == HealthStatus.READY
    smoke_calls = [kwargs for command, kwargs in commands if "--output-last-message" in command]
    assert smoke_calls
    assert smoke_calls[0]["timeout"] == 30.0


class FakeWorkspace:
    async def __aenter__(self) -> "FakeWorkspace":
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="devspace",
            status=BackendHealthStatus.READY,
            user_message="Local workspace is ready.",
        )


class FakeAgentSession:
    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="local_codex_bridge",
            status=BackendHealthStatus.READY,
            user_message="Codex control is ready.",
        )


def test_profile_readiness_uses_codex_runtime_not_agent_health() -> None:
    composition = RuntimeComposition(
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        workspace_factory=lambda: FakeWorkspace(),
        agent_coordinator=object(),
        session_manager=FakeAgentSession(),
        codex_readiness=BackendHealth(
            capability="codex",
            status=BackendHealthStatus.READY,
            user_message="Codex 当前配置可以正常使用。",
        ),
    )

    ready = asyncio.run(composition.readiness())
    assert ready.status == "READY"
    assert ready.codex_status == "READY"
    assert ready.agent_status == "READY"
    assert ready.workspace_status == "READY"
    assert ready.requires_user_action is False

    composition.codex_readiness = BackendHealth(
        capability="codex",
        status=BackendHealthStatus.DEGRADED,
        user_message="Codex 当前凭据不可用。",
    )
    degraded = asyncio.run(composition.readiness())
    assert degraded.status == "DEGRADED"
    assert degraded.codex_status == "DEGRADED"
    assert degraded.requires_user_action is True
