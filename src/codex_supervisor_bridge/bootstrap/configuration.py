from __future__ import annotations

import json
import os
import tempfile
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .paths import AppDataPaths

CONFIG_VERSION = 1


class DevelopmentStyle(str, Enum):
    AUTOMATIC = "automatic"
    WEB_FIRST = "web_first"
    CODEX_FIRST = "codex_first"


class CommandPolicy(str, Enum):
    ASK = "ASK"
    ALLOW = "ALLOW"
    DENY = "DENY"


class BasicSettings(BaseModel):
    development_style: DevelopmentStyle = DevelopmentStyle.AUTOMATIC
    allow_chatgpt_codex_delegation: bool = False
    local_command_policy: CommandPolicy = CommandPolicy.ASK
    automatic_git_commit: bool = False
    automatic_pull_request: bool = False
    project_directory: Path | None = None
    github_connected: bool = False
    codex_enabled: bool = True


class AdvancedSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    executable_paths: dict[str, str] = Field(default_factory=dict)
    process_commands: dict[str, str] = Field(default_factory=dict)
    backend_detail: dict[str, str] = Field(default_factory=dict)
    ports: dict[str, int] = Field(default_factory=dict)
    sqlite_path: Path | None = None
    oauth_detail: dict[str, str] = Field(default_factory=dict)
    tunnel_detail: dict[str, str] = Field(default_factory=dict)
    heartbeat_seconds: int = Field(default=30, ge=1, le=3600)
    startup_timeout_seconds: int = Field(default=15, ge=1, le=300)
    shutdown_timeout_seconds: int = Field(default=10, ge=1, le=300)
    log_level: str = "INFO"

    @field_validator("process_commands", "oauth_detail", "tunnel_detail")
    @classmethod
    def reject_credentials(cls, value: dict[str, str]) -> dict[str, str]:
        sensitive = ("token", "secret", "password", "bearer", "credential")
        for key, item in value.items():
            if any(marker in key.lower() for marker in sensitive) or any(
                marker in item.lower() for marker in ("bearer ", "access_token=", "refresh_token=", "password=")
            ):
                raise ValueError("credentials must be stored through SecretStore")
        return value


class AppConfig(BaseModel):
    config_version: int = CONFIG_VERSION
    basic: BasicSettings = Field(default_factory=BasicSettings)
    advanced: AdvancedSettings = Field(default_factory=AdvancedSettings)

    @classmethod
    def safe_defaults(cls, paths: AppDataPaths | None = None) -> "AppConfig":
        paths = paths or AppDataPaths.from_environment()
        return cls(advanced=AdvancedSettings(sqlite_path=paths.database))

    def basic_view(self) -> dict[str, Any]:
        return self.basic.model_dump(mode="json")

    def advanced_view(self) -> dict[str, Any]:
        return self.advanced.model_dump(mode="json")


class ConfigLoadResult(BaseModel):
    config: AppConfig
    status: str = "READY"
    migrated: bool = False
    error: str | None = None


class ConfigStore:
    def __init__(self, path: str | Path | None = None, *, paths: AppDataPaths | None = None) -> None:
        self.paths = paths or AppDataPaths.from_environment()
        self.path = Path(path) if path is not None else self.paths.settings

    def load(self) -> ConfigLoadResult:
        if not self.path.exists():
            return ConfigLoadResult(config=AppConfig.safe_defaults(self.paths))
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            migrated, changed = self._migrate(raw)
            config = AppConfig.model_validate(migrated)
        except (OSError, json.JSONDecodeError, ValidationError, TypeError, ValueError):
            return ConfigLoadResult(
                config=AppConfig.safe_defaults(self.paths),
                status="DEGRADED",
                error="Configuration is invalid; safe defaults are active.",
            )
        if changed:
            self.save(config)
        return ConfigLoadResult(config=config, migrated=changed)

    def save(self, config: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(config.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _migrate(raw: Any) -> tuple[dict[str, Any], bool]:
        if not isinstance(raw, dict):
            raise ValueError("configuration root must be an object")
        version = raw.get("config_version", raw.get("version", 0))
        if not isinstance(version, int) or version < 0 or version > CONFIG_VERSION:
            raise ValueError("unsupported configuration version")
        migrated = dict(raw)
        changed = version != CONFIG_VERSION or "version" in migrated
        if "version" in migrated:
            migrated.pop("version", None)
        if version == 0:
            basic = dict(migrated.get("basic", {}))
            if "default_development_style" in migrated:
                basic.setdefault("development_style", migrated.pop("default_development_style"))
            if "project_directory" in migrated:
                basic.setdefault("project_directory", migrated.pop("project_directory"))
            migrated["basic"] = basic
        migrated["config_version"] = CONFIG_VERSION
        return migrated, changed
