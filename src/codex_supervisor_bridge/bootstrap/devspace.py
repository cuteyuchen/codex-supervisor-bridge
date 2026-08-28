from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .auth import FirstAuthorizationFlow
from .paths import AppDataPaths
from .process import ManagedProcessSpec
from .secrets import SecretStore

DEVSPACE_TESTED_VERSIONS = ("1.0.5", "1.0.8")
DEVSPACE_SUPPORTED_VERSION_RANGE = ">=1.0.5,<1.1"


class DevSpaceVersionCompatibility:
    """Map a DevSpace release to the exact configuration layout it reads."""

    @classmethod
    def parse_version(cls, value: str | None) -> tuple[int, ...] | None:
        if not value:
            return None
        match = re.search(r"(\d+(?:\.\d+)+)", value)
        if match is None:
            return None
        try:
            return tuple(int(part) for part in match.group(1).split("."))
        except ValueError:
            return None

    @classmethod
    def is_supported(cls, version: str | None) -> bool:
        parsed = cls.parse_version(version)
        return parsed is not None and (1, 0, 5) <= parsed < (1, 1)

    @classmethod
    def uses_flat_json(cls, version: str | None) -> bool:
        parsed = cls.parse_version(version)
        return parsed is not None and (1, 0, 5) <= parsed < (1, 1)

class DevSpaceBootstrapConfig(BaseModel):
    """DevSpace 1.0.x released contract, kept at the provider boundary."""

    schema_variant: Literal["v1_0_flat"] = "v1_0_flat"
    port: int = Field(ge=1, le=65535)
    allowed_roots: list[Path] = Field(default_factory=list)
    worktree_root: Path
    state_dir: Path
    public_base_url: str | None = None
    owner_secret_ref: str = "devspace-owner-token"

    def document(self) -> dict[str, object]:
        if self.schema_variant != "v1_0_flat":
            raise ValueError("unsupported DevSpace configuration schema")
        return {
            "host": "127.0.0.1",
            "port": self.port,
            "allowedRoots": [str(path) for path in self.allowed_roots],
            "publicBaseUrl": self.public_base_url,
            "allowedHosts": ["localhost", "127.0.0.1"],
            "stateDir": str(self.state_dir),
            "worktreeRoot": str(self.worktree_root),
            "artifactsEnabled": False,
            "agentDir": "~/.codex",
            "subagents": False,
        }

    def upstream_compatibility(self) -> dict[str, Any]:
        return {
            "tested_versions": list(DEVSPACE_TESTED_VERSIONS),
            "supported_version_range": DEVSPACE_SUPPORTED_VERSION_RANGE,
            "configuration_kind": "v1_0_flat",
            "configuration_file": "config.json",
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
        return self.config_directory / "config.json"

    @property
    def auth_path(self) -> Path:
        return self.config_directory / "auth.json"

    def prepare_auth(self, secret_store: SecretStore) -> Path:
        owner_token = secret_store.get(self.config.owner_secret_ref)
        if not owner_token:
            owner_token = secrets.token_urlsafe(32)
            secret_store.set(self.config.owner_secret_ref, owner_token)
        if len(owner_token) < 16:
            raise ValueError("DevSpace owner credential is too short")
        self.config_directory.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="devspace-auth-", suffix=".tmp", dir=self.config_directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"ownerToken": owner_token}, handle)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.auth_path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.auth_path

    def write_config(self) -> Path:
        self.config_directory.mkdir(parents=True, exist_ok=True)
        # DevSpace 1.0.x ignores this file when config.json exists. Keep the
        # managed directory single-source if an earlier P6.6 prototype left one.
        (self.config_directory / "config.jsonc").unlink(missing_ok=True)
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
