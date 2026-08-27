from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, Field

from .auth import FirstAuthorizationFlow
from .paths import AppDataPaths
from .process import ManagedProcessSpec
from .secrets import SecretStore


class DevSpaceBootstrapConfig(BaseModel):
    """The current DevSpace v1 config shape, kept at the provider boundary."""

    config_version: int = 1
    port: int = Field(ge=1, le=65535)
    allowed_roots: list[Path] = Field(default_factory=list)
    worktree_root: Path
    state_dir: Path
    public_base_url: str | None = None
    owner_secret_ref: str = "devspace-owner-token"

    def document(self) -> dict[str, object]:
        return {
            "$schema": "https://raw.githubusercontent.com/Waishnav/devspace/main/schema/v1/devspace.schema.json",
            "configVersion": self.config_version,
            "server": {
                "host": "127.0.0.1",
                "port": self.port,
                "publicBaseUrl": self.public_base_url,
                "allowedHosts": [],
                "trustProxy": False,
            },
            "workspaces": {
                "allowedRoots": [str(path) for path in self.allowed_roots],
                "worktreeRoot": str(self.worktree_root),
            },
            "storage": {"stateDir": str(self.state_dir)},
            "tools": {"mode": "codex"},
            "ui": {"enabled": False},
            "artifacts": {"enabled": False},
            "skills": {"enabled": True, "paths": [], "agentDir": "~/.codex"},
            "subagents": {"enabled": False, "providers": []},
            "logging": {
                "level": "info",
                "format": "json",
                "requests": True,
                "assets": False,
                "toolCalls": True,
                "shellCommands": False,
            },
            "oauth": {
                "accessTokenTtlSeconds": 3600,
                "refreshTokenTtlSeconds": 2592000,
                "scopes": ["devspace"],
                "allowedRedirectHosts": ["chatgpt.com", "localhost", "127.0.0.1"],
            },
        }


class DevSpaceBootstrap:
    def __init__(
        self,
        *,
        paths: AppDataPaths,
        config: DevSpaceBootstrapConfig,
        executable: str = "devspace",
    ) -> None:
        self.paths = paths
        self.config = config
        self.executable = executable

    @classmethod
    def from_app_data(
        cls,
        paths: AppDataPaths,
        *,
        port: int,
        project_directory: Path | None = None,
        executable: str = "devspace",
    ) -> "DevSpaceBootstrap":
        roots = [project_directory.resolve()] if project_directory else []
        return cls(
            paths=paths,
            config=DevSpaceBootstrapConfig(
                port=port,
                allowed_roots=roots,
                worktree_root=paths.cache / "devspace" / "worktrees",
                state_dir=paths.data / "devspace",
            ),
            executable=executable,
        )

    @property
    def config_directory(self) -> Path:
        return self.paths.config / "devspace"

    @property
    def config_path(self) -> Path:
        return self.config_directory / "config.jsonc"

    def write_config(self) -> Path:
        self.config_directory.mkdir(parents=True, exist_ok=True)
        self.config.worktree_root.mkdir(parents=True, exist_ok=True)
        self.config.state_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.config.document(), indent=2, sort_keys=True) + "\n"
        fd, temporary = tempfile.mkstemp(prefix="devspace-", suffix=".tmp", dir=self.config_directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.config_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.config_path

    def process_spec(self, *, startup_timeout: float = 15.0, shutdown_timeout: float = 10.0) -> ManagedProcessSpec:
        return ManagedProcessSpec(
            name="devspace",
            command=[self.executable, "serve"],
            cwd=self.config_directory,
            env={**os.environ, "DEVSPACE_CONFIG_DIR": str(self.config_directory)},
            startup_timeout=startup_timeout,
            shutdown_timeout=shutdown_timeout,
        )

    def authorization_flow(self, secret_store: SecretStore, *, authorization_url: str) -> FirstAuthorizationFlow:
        return FirstAuthorizationFlow("DevSpace", authorization_url, secret_store)
