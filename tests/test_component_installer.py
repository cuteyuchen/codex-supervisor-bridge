from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from codex_supervisor_bridge.bootstrap.installer import (
    ComponentInstaller,
    ComponentManifest,
)


def manifest(
    name: str = "local-codex-bridge",
    *,
    version: str = "2.1.3",
    checksum: str | None = None,
    install_commands: list[str] | None = None,
) -> ComponentManifest:
    payload = b"component-archive"
    return ComponentManifest(
        name=name,
        display_name="Codex control",
        version=version,
        source="https://example.invalid/component.tgz",
        source_ref="v2.1.3",
        checksum_sha256=checksum or hashlib.sha256(payload).hexdigest(),
        install_commands=install_commands or ["npm ci && npm run build"],
        requires_node=True,
    )


def test_installer_atomic_promote_and_current_pointer(tmp_path: Path) -> None:
    commands: list[tuple[str, Path]] = []
    downloaded: list[tuple[str, Path]] = []

    def downloader(url: str, destination: Path) -> bytes:
        downloaded.append((url, destination))
        (destination / "component.txt").write_text("v2.1.3", encoding="utf-8")
        return b"component-archive"

    def runner(command: str, cwd: Path) -> int:
        commands.append((command, cwd))
        return 0

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=runner,
        max_retries=2,
    )
    result = installer.install(manifest())

    assert result.status == "INSTALLED"
    assert result.retry_count == 1
    assert result.installed_path is not None
    assert result.installed_path.is_dir()
    assert (result.installed_path / "component.txt").read_text(encoding="utf-8") == "v2.1.3"
    pointer = json.loads(
        (tmp_path / "components" / "local-codex-bridge" / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["version"] == "2.1.3"
    assert pointer["path"] == str(result.installed_path)
    assert commands and commands[0][0] == "npm ci && npm run build"


def test_installer_checksum_mismatch_fails_closed(tmp_path: Path) -> None:
    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=lambda url, destination: b"tampered",
        runner=lambda command, cwd: 0,
        max_retries=1,
    )
    result = installer.install(manifest(checksum="0" * 64))

    assert result.status == "FAILED"
    assert "checksum mismatch" in (result.error or "")
    assert not (tmp_path / "components" / "local-codex-bridge" / "current.json").exists()


def test_installer_rolls_back_to_previous_version(tmp_path: Path) -> None:
    state: dict[str, Any] = {"fail": False}
    commands: list[str] = []

    def downloader(url: str, destination: Path) -> bytes:
        (destination / "component.txt").write_text("content", encoding="utf-8")
        return b"component-archive"

    def runner(command: str, cwd: Path) -> int:
        del cwd
        commands.append(command)
        return 1 if state["fail"] else 0

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=runner,
        max_retries=2,
    )
    first = installer.install(manifest(version="2.1.2"))
    assert first.status == "INSTALLED"
    state["fail"] = True
    second = installer.install(manifest(version="2.1.3"))

    assert second.status == "ROLLED_BACK"
    assert second.retry_count == 2
    pointer = json.loads(
        (tmp_path / "components" / "local-codex-bridge" / "current.json").read_text(encoding="utf-8")
    )
    assert pointer["version"] == "2.1.2"
