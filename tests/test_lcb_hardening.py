from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from codex_supervisor_bridge.bootstrap.installer import ComponentInstaller, ComponentManifest
from codex_supervisor_bridge.bootstrap.lcb_hardening import (
    LCB_HARDENING_REVISION,
    LCB_RUNTIME_CONTRACT,
    LCB_RUNTIME_MARKER,
    LcbHardeningError,
    apply_lcb_runtime_hardening,
    has_lcb_runtime_hardening,
    require_lcb_runtime_hardening,
)
from tests.lcb_fixtures import (
    UPSTREAM_LCB_APP_SERVER_SOURCE,
    write_upstream_lcb_repository,
)


def _lcb_archive(root_name: str = "Local-Codex-Bridge-test") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr(f"{root_name}/src/app-server.ts", UPSTREAM_LCB_APP_SERVER_SOURCE)
        archive.writestr(f"{root_name}/dist/src/index.js", "// built fixture\n")
    return stream.getvalue()


def _manifest() -> ComponentManifest:
    return ComponentManifest(
        name="local-codex-bridge",
        display_name="Codex control",
        version="2.1.3",
        source="https://example.invalid/local-codex-bridge.zip",
        source_ref="4ffed814f615316ade8967189a2e1772488d33c2",
        archive_kind="zip",
        archive_root="Local-Codex-Bridge-test",
        entrypoint="dist/src/index.js",
        install_commands=[["npm", "run", "build"]],
        source_patch=LCB_RUNTIME_CONTRACT,
    )


def test_lcb_hardening_is_idempotent_and_binds_both_source_digests(tmp_path: Path) -> None:
    repository = write_upstream_lcb_repository(tmp_path / "Local-Codex-Bridge")

    first = apply_lcb_runtime_hardening(repository)
    first_payload = json.loads(first.read_text(encoding="utf-8"))
    first_app_server = (repository / "src" / "app-server.ts").read_text(encoding="utf-8")
    second = apply_lcb_runtime_hardening(repository)

    assert second == first
    assert has_lcb_runtime_hardening(repository) is True
    assert first_payload["contract"] == LCB_RUNTIME_CONTRACT
    assert first_payload["hardening_revision"] == LCB_HARDENING_REVISION
    assert set(first_payload["files_sha256"]) == {
        "src/app-server.ts",
        "src/supervisor-runtime.ts",
    }
    assert (repository / "src" / "app-server.ts").read_text(encoding="utf-8") == first_app_server

    runtime_source = repository / "src" / "supervisor-runtime.ts"
    runtime_source.write_text(
        runtime_source.read_text(encoding="utf-8") + "// tampered\n",
        encoding="utf-8",
    )

    assert has_lcb_runtime_hardening(repository) is False
    with pytest.raises(LcbHardeningError, match="LCB_RUNTIME_ISOLATION_UNSUPPORTED"):
        require_lcb_runtime_hardening(repository)


def test_hardened_source_guards_every_destructive_app_server_action(tmp_path: Path) -> None:
    repository = write_upstream_lcb_repository(tmp_path / "Local-Codex-Bridge")
    apply_lcb_runtime_hardening(repository)

    app_server = (repository / "src" / "app-server.ts").read_text(encoding="utf-8")
    runtime = (repository / "src" / "supervisor-runtime.ts").read_text(encoding="utf-8")

    assert "verifyOwnership();\n    child.stdin.end();" in app_server
    assert "verifyOwnership();\n    platformPolicy.softTerminateChild(child);" in app_server
    assert "verifyOwnership();\n    platformPolicy.hardTerminateChild(child);" in app_server
    assert "captureProcessIdentity(child.pid, 20, 25)" in app_server
    assert "Supervisor process identity changed; termination refused" in runtime
    assert "metadata.lcb_runtime_contract !== SUPERVISOR_RUNTIME_CONTRACT" in runtime
    assert "metadata.lcb_hardening_revision !== SUPERVISOR_HARDENING_REVISION" in runtime


def test_ownership_token_reaches_lcb_but_is_removed_from_codex_child(tmp_path: Path) -> None:
    repository = write_upstream_lcb_repository(tmp_path / "Local-Codex-Bridge")
    apply_lcb_runtime_hardening(repository)
    app_server = (repository / "src" / "app-server.ts").read_text(encoding="utf-8")

    assert "readSupervisorRuntimeBinding(sourceEnvironment)" in app_server
    assert "delete childEnvironment.CODEX_SUPERVISOR_OWNERSHIP_TOKEN;" in app_server
    assert "delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_CONTRACT;" in app_server
    assert "delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_METADATA;" in app_server


def test_installer_applies_trusted_hardening_before_build(tmp_path: Path) -> None:
    archive = _lcb_archive()
    build_observations: list[bool] = []

    def downloader(_url: str, destination: Path) -> Path:
        destination.write_bytes(archive)
        return destination

    def runner(_command: list[str], cwd: Path) -> int:
        build_observations.append(has_lcb_runtime_hardening(cwd))
        return 0

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=runner,
        max_retries=1,
    )

    result = installer.install(_manifest())

    assert result.status == "INSTALLED"
    assert build_observations == [True]
    assert result.installed_path is not None
    assert has_lcb_runtime_hardening(result.installed_path) is True


def test_tampered_hardening_cannot_be_reported_as_already_installed(tmp_path: Path) -> None:
    archive = _lcb_archive()
    downloads = 0

    def downloader(_url: str, destination: Path) -> Path:
        nonlocal downloads
        downloads += 1
        destination.write_bytes(archive)
        return destination

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=lambda _command, _cwd: 0,
        max_retries=1,
    )
    first = installer.install(_manifest())
    assert first.installed_path is not None
    (first.installed_path / LCB_RUNTIME_MARKER).write_text("{}\n", encoding="utf-8")

    second = installer.install(_manifest())

    assert first.status == "INSTALLED"
    assert second.status == "INSTALLED"
    assert downloads == 2
    assert second.installed_path is not None
    assert has_lcb_runtime_hardening(second.installed_path) is True
