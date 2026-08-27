from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def default_data_dir() -> Path:
    return Path.home() / ".codex-supervisor-bridge"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    host: str = "127.0.0.1"
    port: int = 8765

    @classmethod
    def from_env(cls) -> "Settings":
        database_path = Path(
            os.getenv("SUPERVISOR_DB_PATH", str(default_data_dir() / "supervisor.db"))
        ).expanduser()
        host = os.getenv("SUPERVISOR_HOST", "127.0.0.1")
        port_text = os.getenv("SUPERVISOR_PORT", "8765")
        try:
            port = int(port_text)
        except ValueError as exc:
            raise ValueError("SUPERVISOR_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise ValueError("SUPERVISOR_PORT must be between 1 and 65535")
        return cls(database_path=database_path, host=host, port=port)
