from __future__ import annotations

import ipaddress
import os
import re
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RemoteAccessMode(str, Enum):
    """Supported remote transports."""

    OPENAI_SECURE_MCP_TUNNEL = "openai_secure_mcp_tunnel"
    GENERIC_HTTPS = "generic_https"
    PRIVATE_GATEWAY = "private_gateway"


class RemoteAccessFailure(str, Enum):
    TUNNEL_NOT_CONFIGURED = "TUNNEL_NOT_CONFIGURED"
    TUNNEL_CLIENT_MISSING = "TUNNEL_CLIENT_MISSING"
    TUNNEL_RUNTIME_KEY_MISSING = "TUNNEL_RUNTIME_KEY_MISSING"
    TUNNEL_PERMISSION_DENIED = "TUNNEL_PERMISSION_DENIED"
    TUNNEL_ID_INVALID = "TUNNEL_ID_INVALID"
    TUNNEL_NOT_FOUND = "TUNNEL_NOT_FOUND"
    TUNNEL_CONTROL_PLANE_UNREACHABLE = "TUNNEL_CONTROL_PLANE_UNREACHABLE"
    LOCAL_MCP_UNREACHABLE = "LOCAL_MCP_UNREACHABLE"
    LOCAL_MCP_PROTOCOL_ERROR = "LOCAL_MCP_PROTOCOL_ERROR"
    TUNNEL_PROCESS_CRASHED = "TUNNEL_PROCESS_CRASHED"
    TUNNEL_NOT_READY = "TUNNEL_NOT_READY"
    READY = "READY"


class RemoteAccessHealth(BaseModel):
    """Non-secret health evidence for a remote access backend."""

    model_config = ConfigDict(extra="allow")

    provider: RemoteAccessMode | str
    process_running: bool = False
    healthy: bool = False
    ready: bool = False
    state: str = RemoteAccessFailure.TUNNEL_NOT_CONFIGURED.value
    connection_state: str | None = None
    health_url: str | None = None
    local_mcp_url: str | None = None
    process_identity: dict[str, Any] | None = None
    client_version: str | None = None
    runtime_key_present: bool = False
    technical_detail: str | None = None

    @property
    def status(self) -> str:
        return self.connection_state or self.state


class RemoteAccessConfig(BaseModel):
    """Provider-neutral settings; secret values are never persisted here."""

    model_config = ConfigDict(extra="allow")

    provider: RemoteAccessMode = RemoteAccessMode.OPENAI_SECURE_MCP_TUNNEL
    tunnel_id: str | None = Field(default=None, pattern=r"^tunnel_[A-Za-z0-9_-]+$")
    runtime_secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    local_mcp_url: str = "http://127.0.0.1:8765/mcp"
    health_listener: str = "127.0.0.1:0"
    health_url: str | None = None
    process_identity: dict[str, Any] | None = None
    client_version: str = "0.0.13"
    connection_state: str = RemoteAccessFailure.TUNNEL_NOT_CONFIGURED.value

    @field_validator("local_mcp_url")
    @classmethod
    def validate_local_mcp_url(cls, value: str) -> str:
        _validate_loopback_url(value)
        return value

    @field_validator("health_listener")
    @classmethod
    def validate_health_listener(cls, value: str) -> str:
        _validate_loopback_listener(value)
        return value


class OpenAISecureMcpTunnelConfig(RemoteAccessConfig):
    """Configuration for the official ``openai/tunnel-client`` runtime."""

    provider: RemoteAccessMode = RemoteAccessMode.OPENAI_SECURE_MCP_TUNNEL
    tunnel_id: str = Field(pattern=r"^tunnel_[A-Za-z0-9_-]+$")
    runtime_secret_ref: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    client_version: str = "0.0.13"

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, value: RemoteAccessMode) -> RemoteAccessMode:
        if value is not RemoteAccessMode.OPENAI_SECURE_MCP_TUNNEL:
            raise ValueError("OpenAISecureMcpTunnelConfig requires the OpenAI tunnel provider")
        return value

    @field_validator("client_version")
    @classmethod
    def validate_client_version(cls, value: str) -> str:
        if value != "0.0.13":
            raise ValueError("tunnel-client version is not the P6.6 pinned release")
        return value


class RemoteAccessBackend(Protocol):
    """Lifecycle contract consumed by Bootstrap, Doctor, and Status."""

    def validate(self, config: RemoteAccessConfig) -> list[str]: ...

    def start(self, config: RemoteAccessConfig) -> RemoteAccessHealth: ...

    def stop(self) -> RemoteAccessHealth: ...

    def health(self) -> RemoteAccessHealth: ...


class SecureRemoteAccessConfig(BaseModel):
    """Legacy generic HTTPS model kept for vendor-neutral compatibility."""

    public_url: str | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int | None = Field(default=None, ge=1, le=65535)
    auth_secret_ref: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")
    session_identity: str | None = None


class SecureRemoteAccess(Protocol):
    def validate(self, config: SecureRemoteAccessConfig) -> list[str]: ...

    def start(self, config: SecureRemoteAccessConfig) -> dict[str, str]: ...

    def stop(self) -> None: ...

    def health(self) -> dict[str, str | bool | None]: ...


class SecureRemoteAccessController:
    """Lifecycle shell for the older authenticated HTTPS tunnel provider."""

    def __init__(self) -> None:
        self._config: SecureRemoteAccessConfig | None = None
        self._active = False

    def validate(self, config: SecureRemoteAccessConfig) -> list[str]:
        return SecureRemoteAccessValidator.validate(config)

    def start(self, config: SecureRemoteAccessConfig) -> dict[str, str]:
        errors = self.validate(config)
        if errors:
            raise ValueError("secure remote access prerequisites failed: " + "; ".join(errors))
        self._config = config
        self._active = True
        return {
            "public_url": config.public_url or "",
            "session_identity": config.session_identity or "",
            "status": "connected",
        }

    def stop(self) -> None:
        self._active = False

    def reconnect(self) -> dict[str, str]:
        if self._config is None:
            raise RuntimeError("secure remote access has not been configured")
        self._active = False
        return self.start(self._config)

    def rotate(self, config: SecureRemoteAccessConfig) -> dict[str, str]:
        self.stop()
        return self.start(config)

    def health(self) -> dict[str, str | bool | None]:
        return {
            "active": self._active,
            "public_url": self._config.public_url if self._config else None,
            "session_identity": self._config.session_identity if self._config else None,
        }


class SecureRemoteAccessValidator:
    """Vendor-neutral security gate used before any generic tunnel implementation."""

    @staticmethod
    def validate(config: SecureRemoteAccessConfig) -> list[str]:
        errors: list[str] = []
        if config.bind_host not in {"127.0.0.1", "::1", "localhost"}:
            errors.append("local MCP must bind to loopback")
        if config.public_url is None:
            errors.append("public HTTPS URL is not configured")
        else:
            parsed = urlparse(config.public_url)
            if parsed.scheme != "https" or not parsed.netloc:
                errors.append("remote MCP URL must use HTTPS")
        if not config.auth_secret_ref:
            errors.append("remote MCP authentication is not configured")
        if not config.session_identity:
            errors.append("remote session identity is not configured")
        return errors


class OpenAISecureMcpTunnelValidator:
    """Validation for the official OpenAI Secure MCP Tunnel configuration."""

    @staticmethod
    def validate(config: OpenAISecureMcpTunnelConfig) -> list[str]:
        errors: list[str] = []
        try:
            _validate_loopback_url(config.local_mcp_url)
        except ValueError as exc:
            errors.append(str(exc))
        try:
            _validate_loopback_listener(config.health_listener)
        except ValueError as exc:
            errors.append(str(exc))
        if not re.fullmatch(r"tunnel_[A-Za-z0-9_-]+", config.tunnel_id):
            errors.append(RemoteAccessFailure.TUNNEL_ID_INVALID.value)
        if config.client_version != "0.0.13":
            errors.append("tunnel-client version is not the P6.6 pinned release")
        return errors


class OpenAISecureMcpTunnelController:
    """Bridge-owned foreground ``tunnel-client run`` process.

    The runtime key is injected only into the child environment. The command
    line uses an ``env:`` reference, so argv, process state, and diagnostics do
    not contain the key value.
    """

    process_name = "openai-tunnel-client"

    def __init__(
        self,
        *,
        process_manager: Any | None = None,
        secret_store: Any | None = None,
        executable: str | Path | None = None,
        runtime_dir: str | Path | None = None,
        client_version: str = "0.0.13",
    ) -> None:
        self.process_manager = process_manager
        self.secret_store = secret_store
        self.executable = str(executable) if executable else "tunnel-client"
        self.runtime_dir = Path(runtime_dir) if runtime_dir else None
        self.client_version = client_version
        self._config: OpenAISecureMcpTunnelConfig | None = None
        self._health = RemoteAccessHealth(
            provider=RemoteAccessMode.OPENAI_SECURE_MCP_TUNNEL,
            client_version=client_version,
        )

    def validate(self, config: RemoteAccessConfig) -> list[str]:
        if not isinstance(config, OpenAISecureMcpTunnelConfig):
            try:
                config = OpenAISecureMcpTunnelConfig.model_validate(config.model_dump())
            except Exception as exc:  # noqa: BLE001 - convert to user-safe gate
                return [RemoteAccessFailure.TUNNEL_NOT_CONFIGURED.value, str(exc)]
        return OpenAISecureMcpTunnelValidator.validate(config)

    def build_command(self, config: OpenAISecureMcpTunnelConfig, health_url_file: Path) -> list[str]:
        errors = self.validate(config)
        if errors:
            raise ValueError("; ".join(errors))
        return [
            self.executable,
            "run",
            "--control-plane.tunnel-id",
            config.tunnel_id,
            "--control-plane.api-key",
            "env:CODEX_SUPERVISOR_TUNNEL_RUNTIME_KEY",
            "--mcp.server-url",
            f"url={config.local_mcp_url}",
            "--health.listen-addr",
            config.health_listener,
            "--health.url-file",
            str(health_url_file),
            "--log.level",
            "info",
        ]

    def start(
        self,
        config: RemoteAccessConfig,
        *,
        supervisor_ready: bool = True,
    ) -> RemoteAccessHealth:
        if not supervisor_ready:
            return self._set_health(
                state=RemoteAccessFailure.LOCAL_MCP_UNREACHABLE.value,
                technical_detail="Supervisor MCP must be READY before tunnel start",
            )
        if not isinstance(config, OpenAISecureMcpTunnelConfig):
            try:
                config = OpenAISecureMcpTunnelConfig.model_validate(config.model_dump())
            except Exception:
                return self._set_health(state=RemoteAccessFailure.TUNNEL_NOT_CONFIGURED.value)
        errors = self.validate(config)
        if errors:
            code = (
                RemoteAccessFailure.TUNNEL_ID_INVALID.value
                if any("tunnel" in item.lower() or "TUNNEL_ID_INVALID" in item for item in errors)
                else RemoteAccessFailure.TUNNEL_NOT_CONFIGURED.value
            )
            return self._set_health(state=code, technical_detail="; ".join(errors))
        if self.secret_store is None:
            return self._set_health(state=RemoteAccessFailure.TUNNEL_RUNTIME_KEY_MISSING.value)
        try:
            runtime_key = self.secret_store.get(config.runtime_secret_ref)
        except (OSError, RuntimeError, ValueError):
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_RUNTIME_KEY_MISSING.value,
                technical_detail="runtime key reference could not be resolved",
            )
        if not runtime_key:
            return self._set_health(state=RemoteAccessFailure.TUNNEL_RUNTIME_KEY_MISSING.value)
        if self.process_manager is None:
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_NOT_READY.value,
                technical_detail="ProcessManager is not configured",
                runtime_key_present=True,
            )
        from .process import ManagedProcessSpec

        runtime_dir = self.runtime_dir or self.process_manager.runtime_dir
        runtime_dir.mkdir(parents=True, exist_ok=True)
        health_url_file = runtime_dir / "openai-tunnel-health.url"
        health_url_file.unlink(missing_ok=True)
        env = {**os.environ, "CODEX_SUPERVISOR_TUNNEL_RUNTIME_KEY": runtime_key}
        command = self.build_command(config, health_url_file)
        try:
            state = self.process_manager.start(
                ManagedProcessSpec(
                    name=self.process_name,
                    command=command,
                    env=env,
                    startup_timeout=30.0,
                    shutdown_timeout=10.0,
                    readiness_probe=lambda: self._probe_ready(health_url_file),
                )
            )
        except FileNotFoundError:
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_CLIENT_MISSING.value,
                runtime_key_present=True,
            )
        except PermissionError:
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_PERMISSION_DENIED.value,
                runtime_key_present=True,
            )
        except OSError as exc:
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_NOT_READY.value,
                runtime_key_present=True,
                technical_detail=f"tunnel client could not start: {type(exc).__name__}",
            )
        self._config = config
        process_running = state.status == "RUNNING"
        health_url = _read_health_url(health_url_file)
        ready = process_running and health_url is not None and _probe_endpoint(health_url, "/readyz")
        healthy = process_running and health_url is not None and _probe_endpoint(health_url, "/healthz")
        state_code = (
            RemoteAccessFailure.READY.value
            if ready and healthy
            else RemoteAccessFailure.TUNNEL_PROCESS_CRASHED.value
            if state.status == "CRASHED"
            else RemoteAccessFailure.TUNNEL_NOT_READY.value
        )
        self._health = RemoteAccessHealth(
            provider=config.provider,
            process_running=process_running,
            healthy=healthy,
            ready=ready,
            state=state_code,
            connection_state=state_code,
            health_url=health_url,
            local_mcp_url=config.local_mcp_url,
            process_identity=state.process_identity,
            client_version=config.client_version,
            runtime_key_present=True,
            technical_detail=state.technical_detail,
        )
        return self._health

    def stop(self) -> RemoteAccessHealth:
        if self.process_manager is not None:
            state = self.process_manager.stop(self.process_name)
            self._health = self._health.model_copy(
                update={
                    "process_running": False,
                    "healthy": False,
                    "ready": False,
                    "state": RemoteAccessFailure.TUNNEL_NOT_READY.value,
                    "connection_state": RemoteAccessFailure.TUNNEL_NOT_READY.value,
                    "process_identity": state.process_identity,
                }
            )
        return self._health

    def health(self) -> RemoteAccessHealth:
        if self.process_manager is None:
            return self._health
        state = self.process_manager.health(self.process_name)
        if state.status == "CRASHED":
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_PROCESS_CRASHED.value,
                process_running=False,
                process_identity=state.process_identity,
            )
        if state.status in {"STOPPED", "STALE", "UNAVAILABLE", "UNKNOWN"}:
            return self._set_health(
                state=RemoteAccessFailure.TUNNEL_NOT_READY.value,
                process_running=False,
                process_identity=state.process_identity,
            )
        health_url = self._health.health_url
        if health_url is None and self.runtime_dir is not None:
            health_url = _read_health_url(self.runtime_dir / "openai-tunnel-health.url")
        healthy = bool(health_url and _probe_endpoint(health_url, "/healthz"))
        ready = bool(health_url and _probe_endpoint(health_url, "/readyz"))
        return self._set_health(
            state=RemoteAccessFailure.READY.value if healthy and ready else RemoteAccessFailure.TUNNEL_NOT_READY.value,
            process_running=state.status == "RUNNING",
            healthy=healthy,
            ready=ready,
            health_url=health_url,
            process_identity=state.process_identity,
        )

    def _probe_ready(self, health_url_file: Path) -> bool:
        url = _read_health_url(health_url_file)
        return bool(url and _probe_endpoint(url, "/healthz") and _probe_endpoint(url, "/readyz"))

    def _set_health(self, **updates: Any) -> RemoteAccessHealth:
        if "state" in updates and "connection_state" not in updates:
            updates["connection_state"] = updates["state"]
        self._health = self._health.model_copy(update=updates)
        return self._health


def _validate_loopback_url(value: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "http" or not parsed.hostname or not _is_loopback(parsed.hostname):
        raise ValueError("LOCAL_MCP_UNREACHABLE: local MCP must use an http loopback URL")
    if parsed.username or parsed.password:
        raise ValueError("local MCP URL must not contain credentials")
    if parsed.path != "/mcp":
        raise ValueError("LOCAL_MCP_PROTOCOL_ERROR: local MCP URL must end with /mcp")


def _validate_loopback_listener(value: str) -> None:
    if ":" not in value:
        raise ValueError("health listener must be host:port")
    host, port = value.rsplit(":", 1)
    if not _is_loopback(host) or not port.isdigit() or not 0 <= int(port) <= 65535:
        raise ValueError("health listener must bind to loopback")


def _is_loopback(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _read_health_url(path: Path) -> str | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _probe_endpoint(base_url: str, path: str) -> bool:
    try:
        parsed = urlparse(base_url)
        if not parsed.hostname or not _is_loopback(parsed.hostname):
            return False
        request = urllib.request.Request(base_url.rstrip("/") + path, method="GET")
        with urllib.request.urlopen(request, timeout=1.5) as response:  # noqa: S310 - loopback checked above
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False
