from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field


class SecureRemoteAccessConfig(BaseModel):
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
    """Lifecycle shell for any authenticated HTTPS tunnel provider."""

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
    """Vendor-neutral security gate used before any tunnel implementation."""

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
