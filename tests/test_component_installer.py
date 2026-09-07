from __future__ import annotations

import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

import codex_supervisor_bridge.bootstrap.installer as installer_module
from codex_supervisor_bridge.bootstrap import (
    ManagedComponentRegistry,
)
from codex_supervisor_bridge.bootstrap.configuration import ConfigStore
from codex_supervisor_bridge.bootstrap.installer import (
    ComponentInstaller,
    ComponentManifest,
)
from codex_supervisor_bridge.bootstrap.models import (
    ComponentHealth,
    DoctorStatus,
    HealthStatus,
)
from codex_supervisor_bridge.bootstrap.paths import AppDataPaths
from codex_supervisor_bridge.bootstrap.repair import RepairService
from codex_supervisor_bridge.bootstrap.secrets import MemorySecretStore
from tests.lcb_fixtures import UPSTREAM_LCB_APP_SERVER_SOURCE, write_fake_lcb_build


def _zip_payload(component: str = "component") -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as handle:
        handle.writestr(f"{component}/component.txt", "v2.1.3")
    return stream.getvalue()


def manifest(
    name: str = "local-codex-bridge",
    *,
    version: str = "2.1.3",
    checksum: str | None = None,
    install_commands: list[list[str]] | None = None,
) -> ComponentManifest:
    payload = _zip_payload()
    return ComponentManifest(
        name=name,
        display_name="Codex control",
        version=version,
        source="https://example.invalid/component.tgz",
        source_ref="v2.1.3",
        checksum_sha256=checksum or hashlib.sha256(payload).hexdigest(),
        archive_kind="zip",
        archive_root="component",
        install_commands=install_commands or [["npm", "ci"], ["npm", "run", "build"]],
        requires_node=True,
    )


def test_installer_atomic_promote_and_current_pointer(tmp_path: Path) -> None:
    commands: list[tuple[list[str], Path]] = []
    downloaded: list[tuple[str, Path]] = []

    def downloader(url: str, destination: Path) -> Path:
        downloaded.append((url, destination))
        destination.write_bytes(_zip_payload())
        return destination

    def runner(command: list[str], cwd: Path) -> int:
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
    assert commands and commands[0][0] == ["npm", "ci"]


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
    commands: list[list[str]] = []

    def downloader(url: str, destination: Path) -> Path:
        destination.write_bytes(_zip_payload())
        return destination

    def runner(command: list[str], cwd: Path) -> int:
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


def test_installer_same_version_reinstall_is_idempotent(tmp_path: Path) -> None:
    calls = {"download": 0, "runner": 0}

    def downloader(url: str, destination: Path) -> Path:
        del url
        calls["download"] += 1
        destination.write_bytes(_zip_payload())
        return destination

    def runner(command: list[str], cwd: Path) -> int:
        del command, cwd
        calls["runner"] += 1
        return 0

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=runner,
        max_retries=1,
    )
    first = installer.install(manifest())
    second = installer.install(manifest())

    assert first.status == "INSTALLED"
    assert second.status == "ALREADY_INSTALLED"
    assert calls == {"download": 1, "runner": 2}


def test_installer_reinstalls_same_version_when_marker_is_damaged(tmp_path: Path) -> None:
    calls = {"download": 0}

    def downloader(url: str, destination: Path) -> Path:
        del url
        calls["download"] += 1
        destination.write_bytes(_zip_payload())
        return destination

    def runner(command: list[str], cwd: Path) -> int:
        del command, cwd
        return 0

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=runner,
        max_retries=1,
    )
    first = installer.install(manifest())
    (first.installed_path / ".codex-supervisor-installed.json").unlink()
    second = installer.install(manifest())

    assert second.status == "INSTALLED"
    assert calls["download"] == 2


def test_installer_recovers_interrupted_staging(tmp_path: Path) -> None:
    def downloader(url: str, destination: Path) -> Path:
        del url
        destination.write_bytes(_zip_payload())
        return destination

    installer = ComponentInstaller(
        tmp_path / "components",
        downloader=downloader,
        runner=lambda command, cwd: 0,
        max_retries=1,
    )
    stale = (
        tmp_path
        / "components"
        / "local-codex-bridge"
        / ".staging"
        / "interrupted"
    )
    stale.mkdir(parents=True)
    (stale / "partial").write_text("partial", encoding="utf-8")

    result = installer.install(manifest())

    assert result.status == "INSTALLED"
    assert not (tmp_path / "components" / "local-codex-bridge" / ".staging").exists()


def test_installer_uses_managed_node_for_npm_commands(tmp_path: Path) -> None:
    components = tmp_path / "components"
    node_root = components / "nodejs" / "24.20.0"
    node_root.mkdir(parents=True)
    (node_root / "node.exe").write_text("node", encoding="utf-8")
    npm_script = node_root / "node_modules" / "npm" / "bin" / "npm-cli.js"
    npm_script.parent.mkdir(parents=True)
    npm_script.write_text("npm", encoding="utf-8")
    node_manifest = manifest(name="nodejs", version="24.20.0")
    node_manifest = node_manifest.model_copy(
        update={"source_ref": "v24.20.0", "requires_node": False}
    )
    (components / "nodejs").mkdir(parents=True, exist_ok=True)
    (components / "nodejs" / "current.json").write_text(
        json.dumps(
            {
                "name": "nodejs",
                "version": "24.20.0",
                "path": str(node_root),
                "manifest": node_manifest.model_dump(mode="json"),
            }
        ),
        encoding="utf-8",
    )
    commands: list[list[str]] = []

    def downloader(url: str, destination: Path) -> Path:
        del url
        destination.write_bytes(_zip_payload())
        return destination

    def runner(command: list[str], cwd: Path) -> int:
        del cwd
        commands.append(command)
        return 0

    installer = ComponentInstaller(
        components,
        downloader=downloader,
        runner=runner,
        max_retries=1,
    )
    result = installer.install(manifest(install_commands=[["npm", "ci"]]))

    assert result.status == "INSTALLED"
    assert commands[0][:2] == [str(node_root / "node.exe"), str(npm_script)]
    assert commands[0][2:] == ["ci"]


def test_default_install_runner_has_a_bounded_timeout(tmp_path: Path, monkeypatch) -> None:
    observed: dict[str, object] = {}

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.update(kwargs)
        raise subprocess.TimeoutExpired(cmd=command, timeout=kwargs["timeout"])

    monkeypatch.setattr(installer_module.subprocess, "run", run)

    with pytest.raises(RuntimeError, match="install command timed out"):
        ComponentInstaller._default_runner(["npm", "ci"], tmp_path)

    assert observed["timeout"] == installer_module.INSTALL_COMMAND_TIMEOUT_SECONDS


def test_builtin_registry_uses_pinned_versions_and_no_floating_latest() -> None:
    registry = ManagedComponentRegistry()
    node = registry.manifest("nodejs")
    devspace = registry.manifest("devspace")
    lcb = registry.manifest("local-codex-bridge")

    assert node.version == "24.20.0"
    assert node.source.startswith("https://nodejs.org/dist/v24.20.0/")
    assert node.checksum_sha256
    assert "latest" not in node.source.lower()

    assert devspace.version == "1.0.8"
    assert devspace.source.startswith("https://registry.npmjs.org/")
    assert "1.0.8" in devspace.source
    assert devspace.checksum_sha256

    assert lcb.version == "2.1.3"
    assert lcb.commit_sha == "4ffed814f615316ade8967189a2e1772488d33c2"
    assert "4ffed814f615316ade8967189a2e1772488d33c2" in lcb.source
    assert lcb.checksum_sha256 is None
    assert lcb.install_commands == [
        ["npm", "ci"],
        ["npm", "run", "typecheck"],
        ["npm", "run", "build"],
    ]
    assert "immutable GitHub commit archive" in registry.verification_strategy(
        "local-codex-bridge"
    )


def test_builtin_registry_requires_node_for_profile_b_components() -> None:
    registry = ManagedComponentRegistry()
    assert registry.manifest("devspace").requires_node is True
    assert registry.manifest("local-codex-bridge").requires_node is True
    assert registry.manifest("nodejs").requires_node is False


def test_trusted_registry_rejects_user_supplied_manifest(tmp_path: Path) -> None:
    registry = ManagedComponentRegistry()
    installer = ComponentInstaller(
        tmp_path / "components",
        trusted_manifests=registry.manifests(),
        downloader=lambda url, destination: b"payload",
        runner=lambda command, cwd: 0,
    )
    forged = ComponentManifest(
        name="nodejs",
        display_name="Node.js",
        version="99.0.0",
        source="https://evil.example/node.tgz",
        source_ref="99.0.0",
        checksum_sha256="0" * 64,
        install_commands=[["curl", "evil"]],
    )

    with pytest.raises(ValueError, match="not from the Bridge trusted registry"):
        installer.plan(forged)


def test_install_commands_must_be_argv_lists_not_shell_strings() -> None:
    with pytest.raises(ValidationError, match="valid list"):
        ComponentManifest(
            name="bad-component",
            display_name="Bad",
            version="1.0.0",
            source="https://example.invalid/bad.tgz",
            source_ref="1.0.0",
            install_commands=["npm ci && npm run build"],
        )

    valid = ComponentManifest(
        name="ok-component",
        display_name="OK",
        version="1.0.0",
        source="https://example.invalid/ok.tgz",
        source_ref="1.0.0",
        install_commands=[["npm", "ci"]],
    )
    assert valid.install_commands == [["npm", "ci"]]


def test_installer_rejects_path_traversal_outside_components_root(tmp_path: Path) -> None:
    installer = ComponentInstaller(tmp_path / "components")
    outside = tmp_path / "outside"
    outside.mkdir()

    with pytest.raises(ValueError, match="escapes the managed components root"):
        installer._assert_within_root(outside)


def test_repair_exposes_install_plan_only_in_advanced_diagnostics(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )

    class MissingComponentsDoctor:
        def run(self, options: object | None = None) -> DoctorStatus:
            del options
            return DoctorStatus(
                status=HealthStatus.UNAVAILABLE,
                components=[
                    ComponentHealth(
                        capability="Node.js",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Local runtime needs an update.",
                        repairable=True,
                    ),
                    ComponentHealth(
                        capability="Local workspace",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Local workspace is not installed.",
                        repairable=True,
                    ),
                    ComponentHealth(
                        capability="Codex control",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Codex control is not installed.",
                        repairable=True,
                    ),
                ],
            )

    registry = ManagedComponentRegistry()
    installer = ComponentInstaller(
        paths.components,
        trusted_manifests=registry.manifests(),
    )
    service = RepairService(
        paths=paths,
        doctor=MissingComponentsDoctor(),  # type: ignore[arg-type]
        installer=installer,
        registry=registry,
        secret_store=MemorySecretStore(),
    )
    actions = service.repair(project_directory=tmp_path)

    install_actions = [
        action for action in actions if action.action.startswith("install_component:")
    ]
    assert {action.action for action in install_actions} == {
        "install_component:nodejs",
        "install_component:devspace",
        "install_component:local-codex-bridge",
    }
    public = [action.user_view() for action in install_actions]
    assert all(item["action"] == "prepare_local_environment" for item in public)
    assert all(
        "nodejs" not in str(item).lower() and "devspace" not in str(item).lower()
        for item in public
    )
    advanced = [action.advanced for action in install_actions]
    assert all("install_plan" in item and "verification_strategy" in item for item in advanced)
    planned_names = {
        item["install_plan"]["component"]["name"]
        for item in advanced
    }
    assert planned_names == {"nodejs", "devspace", "local-codex-bridge"}


def test_repair_plans_managed_node_even_when_system_node_is_ready(tmp_path: Path) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Windows",
    )

    class SystemNodeDoctor:
        def run(self, options: object | None = None) -> DoctorStatus:
            del options
            return DoctorStatus(
                status=HealthStatus.DEGRADED,
                components=[
                    ComponentHealth(
                        capability="Node.js",
                        status=HealthStatus.READY,
                        user_message="Node.js is ready.",
                        advanced={"executable": "C:/node/node.exe", "version": "v24.9.0"},
                    ),
                    ComponentHealth(
                        capability="Local workspace",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Local workspace is not installed.",
                        repairable=True,
                    ),
                    ComponentHealth(
                        capability="Codex control",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Codex control is not installed.",
                        repairable=True,
                    ),
                ],
            )

    registry = ManagedComponentRegistry()
    service = RepairService(
        paths=paths,
        doctor=SystemNodeDoctor(),  # type: ignore[arg-type]
        installer=ComponentInstaller(
            paths.components,
            trusted_manifests=registry.manifests(),
        ),
        registry=registry,
        secret_store=MemorySecretStore(),
    )

    actions = service.repair(project_directory=tmp_path)
    install_actions = {
        action.action
        for action in actions
        if action.action.startswith("install_component:")
    }

    assert install_actions == {
        "install_component:nodejs",
        "install_component:devspace",
        "install_component:local-codex-bridge",
    }


def test_repair_executes_trusted_install_and_registers_managed_paths(
    tmp_path: Path,
) -> None:
    paths = AppDataPaths.from_environment(
        environ={"CODEX_SUPERVISOR_DATA_DIR": str(tmp_path / "app")},
        system="Linux",
    )

    class MissingComponentsDoctor:
        def run(self, options: object | None = None) -> DoctorStatus:
            del options
            return DoctorStatus(
                status=HealthStatus.UNAVAILABLE,
                components=[
                    ComponentHealth(
                        capability="Node.js",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Local runtime needs an update.",
                        repairable=True,
                    ),
                    ComponentHealth(
                        capability="Local workspace",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Local workspace is not installed.",
                        repairable=True,
                    ),
                    ComponentHealth(
                        capability="Codex control",
                        status=HealthStatus.UNAVAILABLE,
                        user_message="Codex control is not installed.",
                        repairable=True,
                    ),
                ],
            )

    registry = ManagedComponentRegistry()
    manifests = registry.manifests()
    manifests["nodejs"] = manifests["nodejs"].model_copy(
        update={"checksum_sha256": None}
    )
    manifests["devspace"] = manifests["devspace"].model_copy(
        update={
            "checksum_sha256": None,
            "archive_kind": "zip",
        }
    )
    manifests["local-codex-bridge"] = manifests["local-codex-bridge"].model_copy(
        update={
            "archive_kind": "zip",
            "archive_root": (
                "Local-Codex-Bridge-4ffed814f615316ade8967189a2e1772488d33c2"
            ),
        }
    )
    registry = ManagedComponentRegistry(manifests)

    def downloader(url: str, destination: Path) -> Path:
        del url
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w") as handle:
            if "nodejs" in str(destination):
                root = "node-v24.20.0-win-x64"
                files = ["node.exe"]
            elif "devspace" in str(destination):
                root = "package"
                files = ["dist/cli.js"]
            else:
                root = "Local-Codex-Bridge-4ffed814f615316ade8967189a2e1772488d33c2"
                files = ["dist/src/index.js"]
            for relative in files:
                handle.writestr(f"{root}/{relative}", "placeholder")
            if "Local-Codex-Bridge" in root:
                handle.writestr(
                    f"{root}/src/app-server.ts",
                    UPSTREAM_LCB_APP_SERVER_SOURCE,
                )
        destination.write_bytes(stream.getvalue())
        return destination

    def runner(_command: list[str], cwd: Path) -> int:
        if (cwd / "src" / "app-server.ts").is_file():
            write_fake_lcb_build(cwd)
        return 0

    installer = ComponentInstaller(
        paths.components,
        trusted_manifests=registry.manifests(),
        downloader=downloader,
        runner=runner,
        max_retries=1,
    )
    service = RepairService(
        paths=paths,
        doctor=MissingComponentsDoctor(),  # type: ignore[arg-type]
        installer=installer,
        registry=registry,
        secret_store=MemorySecretStore(),
        auto_install=True,
    )
    actions = service.repair(project_directory=tmp_path)

    install_actions = [
        action for action in actions if action.action.startswith("install_component:")
    ]
    assert len(install_actions) == 3
    assert all(action.status == HealthStatus.READY for action in install_actions)
    assert all(action.requires_user_action is False for action in install_actions)
    config = ConfigStore(paths=paths).load().config
    assert Path(config.advanced.executable_paths["node"]).name == "node.exe"
    assert Path(config.advanced.executable_paths["devspace_entrypoint"]).parts[-2:] == (
        "dist",
        "cli.js",
    )
    assert config.advanced.local_codex_repository is not None
    assert (
        config.advanced.local_codex_repository
        / "dist"
        / "src"
        / "index.js"
    ).is_file()
