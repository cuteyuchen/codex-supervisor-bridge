from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AppDataPaths:
    """Persistent application paths, never rooted in a user project checkout."""

    root: Path
    data: Path
    logs: Path
    runtime: Path
    config: Path
    cache: Path

    @classmethod
    def from_environment(
        cls,
        *,
        home: Path | None = None,
        environ: dict[str, str] | None = None,
        system: str | None = None,
    ) -> "AppDataPaths":
        env = os.environ if environ is None else environ
        system_name = system or platform.system()
        override = env.get("CODEX_SUPERVISOR_DATA_DIR", "").strip()
        if override:
            root = Path(override).expanduser()
        elif system_name == "Windows":
            local_app_data = env.get("LOCALAPPDATA", "").strip()
            root = Path(local_app_data) if local_app_data else (home or Path.home()) / "AppData" / "Local"
            root /= "CodexSupervisorBridge"
        elif env.get("XDG_DATA_HOME", "").strip():
            root = Path(env["XDG_DATA_HOME"]) / "codex-supervisor-bridge"
        else:
            root = (home or Path.home()) / ".local" / "share" / "codex-supervisor-bridge"
        return cls(
            root=root,
            data=root / "data",
            logs=root / "logs",
            runtime=root / "runtime",
            config=root / "config",
            cache=root / "cache",
        )

    @property
    def database(self) -> Path:
        return self.data / "supervisor.db"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def generated_mcp_config(self) -> Path:
        return self.config / "mcp.json"

    def ensure_directories(self) -> None:
        for path in (self.root, self.data, self.logs, self.runtime, self.config, self.cache):
            path.mkdir(parents=True, exist_ok=True)
