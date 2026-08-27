from __future__ import annotations

from pathlib import Path

import pytest

from codex_supervisor_bridge.config import Settings
from codex_supervisor_bridge.mcp.server import build_parser


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    database = tmp_path / "custom.db"
    monkeypatch.setenv("SUPERVISOR_DB_PATH", str(database))
    monkeypatch.setenv("SUPERVISOR_HOST", "127.0.0.1")
    monkeypatch.setenv("SUPERVISOR_PORT", "9876")
    monkeypatch.setenv("KANDEV_MCP_URL", "http://127.0.0.1:39000/mcp")

    settings = Settings.from_env()

    assert settings.database_path == database
    assert settings.host == "127.0.0.1"
    assert settings.port == 9876
    assert settings.kandev_mcp_url == "http://127.0.0.1:39000/mcp"


def test_settings_reject_invalid_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUPERVISOR_PORT", "not-a-port")
    with pytest.raises(ValueError, match="must be an integer"):
        Settings.from_env()

    monkeypatch.setenv("SUPERVISOR_PORT", "70000")
    with pytest.raises(ValueError, match="between 1 and 65535"):
        Settings.from_env()


def test_settings_reject_empty_kandev_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KANDEV_MCP_URL", "   ")
    with pytest.raises(ValueError, match="must not be empty"):
        Settings.from_env()


def test_cli_defaults_to_local_streamable_http(tmp_path: Path) -> None:
    settings = Settings(database_path=tmp_path / "server.db")
    parser = build_parser(settings)
    args = parser.parse_args([])

    assert args.database == tmp_path / "server.db"
    assert args.transport == "streamable-http"
    assert args.host == "127.0.0.1"
    assert args.port == 8765
    assert args.mcp_path == "/mcp"
    assert args.kandev_mcp_url == "http://127.0.0.1:38429/mcp"
