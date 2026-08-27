from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from codex_supervisor_bridge.bootstrap.paths import AppDataPaths


def default_data_dir() -> Path:
    return AppDataPaths.from_environment().data


@dataclass(frozen=True)
class Settings:
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8765
    kandev_mcp_url: str = "http://127.0.0.1:38429/mcp"
    devspace_mcp_url: str = "http://127.0.0.1:7676/mcp"
    codex_control_command: str = "codex-control-plane-mcp"

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(
            os.getenv("SUPERVISOR_DB_PATH", str(default_data_dir() / "supervisor.db"))
        ).expanduser()
        host = os.getenv("SUPERVISOR_HOST", "127.0.0.1")
        port_text = os.getenv("SUPERVISOR_PORT", "8765")
        kandev_mcp_url = os.getenv("KANDEV_MCP_URL", "http://127.0.0.1:38429/mcp").strip()
        devspace_mcp_url = os.getenv("DEVSPACE_MCP_URL", "http://127.0.0.1:7676/mcp").strip()
        codex_control_command = os.getenv(
            "CODEX_CONTROL_PLANE_COMMAND",
            "codex-control-plane-mcp",
        ).strip()
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("SUPERVISOR_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SUPERVISOR_PORT must be between 1 and 65535")
        if not kandev_mcp_url:
            raise ValueError("KANDEV_MCP_URL must not be empty")
        if not devspace_mcp_url:
            raise ValueError("DEVSPACE_MCP_URL must not be empty")
        if not codex_control_command:
            raise ValueError("CODEX_CONTROL_PLANE_COMMAND must not be empty")
        return cls(
            database_path=database_path,
            host=host,
            port=port,
            kandev_mcp_url=kandev_mcp_url,
            devspace_mcp_url=devspace_mcp_url,
            codex_control_command=codex_control_command,
        )
