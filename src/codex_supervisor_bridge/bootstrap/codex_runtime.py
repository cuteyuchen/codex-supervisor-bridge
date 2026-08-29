from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import tomllib
from enum import Enum
from pathlib import Path
from typing import Callable, Mapping
from urllib.parse import urlparse

from pydantic import BaseModel

from .models import HealthStatus

ProcessRunner = Callable[..., subprocess.CompletedProcess[str]]

MAX_CODEX_CONFIG_BYTES = 512 * 1024
CODEX_SMOKE_TIMEOUT_SECONDS = 30.0
CODEX_SMOKE_PROMPT = (
    "Inspect the current workspace read-only.\n"
    "Return exactly:\n"
    "CODEX_RUNTIME_READY\n"
    "Do not modify files.\n"
    "Do not execute commands that mutate the workspace."
)


class CodexAuthMode(str, Enum):
    """Provider-neutral authentication categories for advanced diagnostics."""

    CHATGPT_ACCOUNT = "chatgpt_account"
    OPENAI_API_KEY = "openai_api_key"
    PROVIDER_ENV_KEY = "provider_env_key"
    CUSTOM_PROVIDER = "custom_provider"
    LOCAL_NO_AUTH = "local_no_auth"
    AWS_OR_CLOUD_PROVIDER = "aws_or_cloud_provider"
    UNKNOWN = "unknown"


class CodexConfigInspection(BaseModel):
    """Read-only metadata about the current Codex provider configuration."""

    config_path: str | None = None
    model: str | None = None
    provider_type: str | None = None
    provider_name: str | None = None
    wire_api: str | None = None
    credential_source_type: str | None = None
    credential_reference: str | None = None
    credential_present: bool | None = None
    base_url_masked: str | None = None
    auth_mode: CodexAuthMode = CodexAuthMode.UNKNOWN
    requires_openai_account: bool | None = None
    config_health: str = "missing"
    config_error: str | None = None


class CodexRuntimeProbeResult(BaseModel):
    """Bounded, read-only runtime smoke result without secret material."""

    ok: bool = False
    status: HealthStatus = HealthStatus.DEGRADED
    category: str = "NOT_RUN"
    user_message: str = "Codex runtime probe was not completed."
    requires_user_action: bool = False
    repairable: bool = False
    technical_detail: str | None = None


class CodexReadiness(BaseModel):
    status: HealthStatus
    executable: str | None = None
    version: str | None = None
    process_launchable: bool = False
    runtime_ready: bool = False
    workspace_ready: bool = False
    auth_mode: CodexAuthMode = CodexAuthMode.UNKNOWN
    config: CodexConfigInspection | None = None
    runtime_probe: CodexRuntimeProbeResult | None = None
    user_message: str
    technical_detail: str | None = None
    requires_user_action: bool = False
    repairable: bool = False


class CodexConfigInspector:
    """Inspect Codex config.toml without reading or changing credentials.

    Only configuration metadata is retained. An ``env_key`` is stored as a
    variable name and never resolved to its value. ``auth.json`` and other
    credential files are deliberately not opened.
    """

    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.config_path = (
            Path(config_path).expanduser()
            if config_path is not None
            else default_codex_config_path(environ)
        )
        self.environ: Mapping[str, str] = environ if environ is not None else os.environ

    def inspect(self) -> CodexConfigInspection:
        if not self.config_path.is_file():
            return CodexConfigInspection(config_health="missing")
        try:
            size = self.config_path.stat().st_size
            if size > MAX_CODEX_CONFIG_BYTES:
                return CodexConfigInspection(
                    config_path=str(self.config_path),
                    config_health="invalid",
                    config_error="Codex configuration file exceeds the inspection size limit.",
                )
            raw = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
            return CodexConfigInspection(
                config_path=str(self.config_path),
                config_health="invalid",
                config_error="Codex configuration could not be parsed; it was not modified.",
                auth_mode=CodexAuthMode.UNKNOWN,
            )
        if not isinstance(raw, dict):
            return CodexConfigInspection(
                config_path=str(self.config_path),
                config_health="invalid",
                config_error="Codex configuration root must be a TOML table.",
            )

        provider_type = _string_or_none(raw.get("model_provider"))
        model = _string_or_none(raw.get("model"))
        provider_table = raw.get("model_providers")
        provider_config = {}
        if isinstance(provider_table, dict) and provider_type in provider_table:
            nested = provider_table[provider_type]
            if isinstance(nested, dict):
                provider_config = nested

        env_key = _string_or_none(provider_config.get("env_key"))
        base_url = _string_or_none(provider_config.get("base_url"))
        requires_openai = provider_config.get("requires_openai_auth")
        requires_openai = requires_openai if isinstance(requires_openai, bool) else None
        credential_present = env_key in self.environ if env_key else None
        auth_mode = _auth_mode(
            provider_type=provider_type,
            env_key=env_key,
            requires_openai=requires_openai,
            base_url=base_url,
        )
        return CodexConfigInspection(
            config_path=str(self.config_path),
            model=model,
            provider_type=provider_type,
            provider_name=_string_or_none(provider_config.get("name")) or provider_type,
            wire_api=_string_or_none(provider_config.get("wire_api")),
            credential_source_type=_credential_source_type(
                auth_mode,
                env_key=env_key,
            ),
            credential_reference=env_key,
            credential_present=credential_present,
            base_url_masked=_mask_base_url(base_url),
            auth_mode=auth_mode,
            requires_openai_account=requires_openai,
            config_health="ready",
        )


class CodexRuntimeSmokeProbe:
    """Run one bounded, read-only Codex invocation to prove real capability."""

    def __init__(
        self,
        *,
        runner: ProcessRunner | None = None,
        timeout: float = CODEX_SMOKE_TIMEOUT_SECONDS,
    ) -> None:
        self.runner = runner or subprocess.run
        self.timeout = timeout

    def probe(
        self,
        executable: str,
        *,
        workspace: Path | None = None,
    ) -> CodexRuntimeProbeResult:
        with tempfile.TemporaryDirectory(prefix="codex-smoke-") as temporary:
            workdir = Path(workspace) if workspace is not None and workspace.is_dir() else Path(temporary)
            output_file = Path(temporary) / "last-message.txt"
            command = [
                executable,
                "exec",
                CODEX_SMOKE_PROMPT,
                "--sandbox",
                "read-only",
                "--json",
                "--ephemeral",
                "--output-last-message",
                str(output_file),
                "--cd",
                str(workdir),
                "--color",
                "never",
            ]
            if not (workdir / ".git").exists():
                command.append("--skip-git-repo-check")
            try:
                result = self.runner(
                    command,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return CodexRuntimeProbeResult(
                    status=HealthStatus.DEGRADED,
                    category="PROVIDER_TIMEOUT",
                    user_message="Codex 暂时不可用。",
                    technical_detail="runtime smoke timed out",
                )
            except OSError:
                return CodexRuntimeProbeResult(
                    status=HealthStatus.DEGRADED,
                    category="PROVIDER_UNREACHABLE",
                    user_message="Codex 暂时不可用。",
                    technical_detail="runtime smoke could not be launched",
                )

            combined = _bounded_output(result.stdout, result.stderr)
            final_message = _read_smoke_message(output_file)
            if result.returncode == 0 and (
                "CODEX_RUNTIME_READY" in final_message
                or "CODEX_RUNTIME_READY" in combined
            ):
                return CodexRuntimeProbeResult(
                    ok=True,
                    status=HealthStatus.READY,
                    category="SUCCESS",
                    user_message="Codex 当前配置可以正常使用。",
                    technical_detail="read-only runtime smoke returned the expected marker",
                )
            return _classify_smoke_failure(
                returncode=result.returncode,
                output=combined,
                final_message=final_message,
            )


class CodexReadinessDetector:
    """Probe the CLI in layered, bounded, non-mutating ways before selecting Codex."""

    def __init__(
        self,
        *,
        finder: Callable[[str], str | None] = shutil.which,
        runner: ProcessRunner | None = None,
        inspector: CodexConfigInspector | None = None,
        smoke_probe: CodexRuntimeSmokeProbe | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.finder = finder
        self.runner = runner or subprocess.run
        self.environ: Mapping[str, str] = environ if environ is not None else os.environ
        self.inspector = inspector or CodexConfigInspector(environ=self.environ)
        self.smoke_probe = smoke_probe or CodexRuntimeSmokeProbe(runner=self.runner)

    def probe(
        self,
        *,
        executable: str = "codex",
        workspace: Path | None = None,
        config_path: str | Path | None = None,
    ) -> CodexReadiness:
        resolved = executable if Path(executable).is_file() else self.finder(executable)
        if resolved is None:
            return CodexReadiness(
                status=HealthStatus.UNAVAILABLE,
                user_message="Codex 需要安装或完成一次凭据设置。",
                technical_detail="executable not found",
                repairable=True,
            )

        inspection = self.inspector.inspect() if config_path is None else CodexConfigInspector(
            config_path=config_path,
            environ=self.environ,
        ).inspect()

        version = self._run([resolved, "--version"])
        if version.returncode != 0:
            return CodexReadiness(
                status=HealthStatus.DEGRADED,
                executable=resolved,
                process_launchable=False,
                auth_mode=inspection.auth_mode,
                config=inspection,
                user_message="Codex 当前配置无法使用。",
                technical_detail="version command failed",
                repairable=True,
            )

        workspace_ready = workspace is None or (
            workspace.is_dir() and (workspace / ".git").exists()
        )
        if inspection.config_health == "invalid":
            return CodexReadiness(
                status=HealthStatus.DEGRADED,
                executable=resolved,
                version=_first_line(version.stdout or version.stderr),
                process_launchable=True,
                workspace_ready=workspace_ready,
                auth_mode=inspection.auth_mode,
                config=inspection,
                user_message="Codex 当前配置无法使用。",
                technical_detail=inspection.config_error or "Codex configuration is invalid",
                repairable=True,
            )
        if inspection.credential_reference and not inspection.credential_present:
            return CodexReadiness(
                status=HealthStatus.DEGRADED,
                executable=resolved,
                version=_first_line(version.stdout or version.stderr),
                process_launchable=True,
                workspace_ready=workspace_ready,
                auth_mode=inspection.auth_mode,
                config=inspection,
                user_message="Codex 当前凭据不可用。",
                technical_detail=f"missing environment variable: {inspection.credential_reference}",
                requires_user_action=True,
            )

        smoke = self.smoke_probe.probe(
            resolved,
            workspace=workspace if workspace is not None and workspace.is_dir() else None,
        )
        if not workspace_ready:
            return CodexReadiness(
                status=HealthStatus.DEGRADED,
                executable=resolved,
                version=_first_line(version.stdout or version.stderr),
                process_launchable=True,
                runtime_ready=smoke.ok,
                workspace_ready=False,
                auth_mode=inspection.auth_mode,
                config=inspection,
                runtime_probe=smoke,
                user_message="Codex 当前配置可以正常使用，但所选项目不可用。",
                technical_detail="selected project is not a readable Git workspace",
                requires_user_action=True,
            )
        return CodexReadiness(
            status=smoke.status,
            executable=resolved,
            version=_first_line(version.stdout or version.stderr),
            process_launchable=True,
            runtime_ready=smoke.ok,
            workspace_ready=True,
            auth_mode=inspection.auth_mode,
            config=inspection,
            runtime_probe=smoke,
            user_message=smoke.user_message,
            technical_detail=smoke.technical_detail,
            requires_user_action=smoke.requires_user_action,
            repairable=smoke.status == HealthStatus.DEGRADED and not smoke.requires_user_action,
        )

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return self.runner(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return subprocess.CompletedProcess(
                command,
                1,
                "",
                "process could not be launched",
            )


def default_codex_config_path(
    environ: Mapping[str, str] | None = None,
) -> Path:
    environment = environ if environ is not None else os.environ
    home = environment.get("CODEX_HOME")
    if home:
        return Path(home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def codex_environment_references(
    inspection: CodexConfigInspection,
) -> list[str]:
    """Return only environment variable names referenced by Codex config."""

    if inspection.credential_reference:
        return [inspection.credential_reference]
    return []


def _auth_mode(
    *,
    provider_type: str | None,
    env_key: str | None,
    requires_openai: bool | None,
    base_url: str | None,
) -> CodexAuthMode:
    if env_key:
        if provider_type == "openai" or env_key.upper() == "OPENAI_API_KEY":
            return CodexAuthMode.OPENAI_API_KEY
        return CodexAuthMode.PROVIDER_ENV_KEY
    lowered = f"{provider_type or ''}".lower()
    if any(marker in lowered for marker in ("aws", "azure", "bedrock", "vertex")):
        return CodexAuthMode.AWS_OR_CLOUD_PROVIDER
    if _is_local_url(base_url) and requires_openai is not True:
        return CodexAuthMode.LOCAL_NO_AUTH
    if provider_type and provider_type != "openai":
        return CodexAuthMode.CUSTOM_PROVIDER
    if requires_openai is True:
        return CodexAuthMode.CHATGPT_ACCOUNT
    if _is_local_url(base_url):
        return CodexAuthMode.LOCAL_NO_AUTH
    return CodexAuthMode.UNKNOWN


def _credential_source_type(
    auth_mode: CodexAuthMode,
    *,
    env_key: str | None,
) -> str | None:
    if env_key:
        return "environment"
    if auth_mode == CodexAuthMode.CHATGPT_ACCOUNT:
        return "chatgpt_account"
    if auth_mode == CodexAuthMode.LOCAL_NO_AUTH:
        return "none"
    return "unknown"


def _is_local_url(value: str | None) -> bool:
    if not value:
        return False
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local")


def _mask_base_url(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.hostname:
            return None
        host = parsed.hostname
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        try:
            port = parsed.port
        except ValueError:
            port = None
        suffix = f":{port}" if port is not None else ""
        return f"{parsed.scheme.lower()}://{host}{suffix}"
    except ValueError:
        return None
    return None


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _bounded_output(stdout: str, stderr: str) -> str:
    return f"{stdout}\n{stderr}"[:2000]


def _read_smoke_message(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:2000]
    except OSError:
        return ""


def _classify_smoke_failure(
    *,
    returncode: int,
    output: str,
    final_message: str,
) -> CodexRuntimeProbeResult:
    del final_message
    lowered = output.lower()
    if any(
        marker in lowered
        for marker in (
            "401",
            "403",
            "unauthorized",
            "not logged in",
            "login required",
            "invalid api key",
            "authentication",
        )
    ):
        return CodexRuntimeProbeResult(
            status=HealthStatus.DEGRADED,
            category="PROVIDER_AUTH_FAILED",
            user_message="Codex 当前凭据无法使用。",
            requires_user_action=True,
            technical_detail="provider rejected the current credential",
        )
    if any(
        marker in lowered
        for marker in (
            "timed out",
            "timeout",
            "connection refused",
            "connection error",
            "network error",
            "dns",
        )
    ):
        return CodexRuntimeProbeResult(
            status=HealthStatus.DEGRADED,
            category="PROVIDER_TIMEOUT",
            user_message="Codex 暂时不可用。",
            technical_detail="provider network timeout",
        )
    if any(
        marker in lowered
        for marker in (
            "model_not_found",
            "model not found",
            "model_not_available",
            "model not available",
            "model does not exist",
        )
    ):
        return CodexRuntimeProbeResult(
            status=HealthStatus.DEGRADED,
            category="MODEL_UNAVAILABLE",
            user_message="Codex 当前模型配置无法使用。",
            requires_user_action=True,
            technical_detail="configured model is unavailable",
        )
    if "config error" in lowered or "invalid config" in lowered or "toml" in lowered:
        return CodexRuntimeProbeResult(
            status=HealthStatus.DEGRADED,
            category="CONFIG_INVALID",
            user_message="Codex 当前配置无法使用。",
            repairable=True,
            technical_detail="Codex configuration error",
        )
    return CodexRuntimeProbeResult(
        status=HealthStatus.DEGRADED,
        category="UNEXPECTED_RUNTIME_RESULT",
        user_message="Codex 当前配置无法使用。",
        technical_detail=f"runtime smoke exited with code {returncode} without the expected marker",
    )


def _first_line(value: str) -> str:
    return value.strip().splitlines()[0] if value.strip() else "unknown"
