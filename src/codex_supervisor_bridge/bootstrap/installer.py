from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Literal, Mapping

from pydantic import BaseModel, Field, field_validator

from .archive import extract_tar_safe, extract_zip_safe
from .download import HttpsDownloader
from .lcb_hardening import apply_lcb_runtime_hardening, has_lcb_runtime_hardening

INSTALL_COMMAND_TIMEOUT_SECONDS = 300.0


class ComponentManifest(BaseModel):
    """Pinned, user-safe component descriptor managed by the Bridge."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str
    version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
    source: str = Field(pattern=r"^https://")
    source_ref: str
    commit_sha: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{40}$",
    )
    checksum_sha256: str | None = None
    checksum_source: str | None = Field(default=None, pattern=r"^https://")
    checksum_entry: str | None = None
    archive_kind: Literal["auto", "zip", "tgz"] = "auto"
    archive_root: str | None = None
    entrypoint: str | None = None
    version_args: list[str] = Field(default_factory=list)
    version_contains: str | None = None
    install_commands: list[list[str]] = Field(default_factory=list)
    requires_node: bool = False
    source_patch: str | None = None

    @field_validator("install_commands")
    @classmethod
    def reject_shell_strings(cls, value: list[list[str]]) -> list[list[str]]:
        for command in value:
            if not command or not command[0].strip():
                raise ValueError("install commands must be non-empty argv lists")
            if any(not isinstance(part, str) for part in command):
                raise ValueError("install commands must contain only strings")
        return value


class InstallPlan(BaseModel):
    component: ComponentManifest
    target_dir: Path
    staging_dir: Path
    artifact_name: str
    max_retries: int = Field(default=3, ge=1, le=10)


class InstallResult(BaseModel):
    component: ComponentManifest
    installed_path: Path | None = None
    status: str
    error: str | None = None
    retry_count: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class InstallerOptions:
    downloader: Callable[[str, Path], Path | bytes] | None = None
    runner: Callable[[list[str], Path], int] | None = None
    max_retries: int = 3


class ComponentInstaller:
    """App-managed dependency installer with atomic promotion and rollback."""

    def __init__(
        self,
        components_root: str | Path,
        *,
        downloader: Callable[[str, Path], Path | bytes] | None = None,
        runner: Callable[[list[str], Path], int] | None = None,
        max_retries: int = 3,
        trusted_manifests: Mapping[str, ComponentManifest] | None = None,
    ) -> None:
        self.components_root = Path(components_root)
        self.components_root.mkdir(parents=True, exist_ok=True)
        self._downloader = downloader or self._default_downloader
        self._runner = runner or self._default_runner
        self.max_retries = max_retries
        self._trusted = dict(trusted_manifests or {})

    def plan(self, manifest: ComponentManifest) -> InstallPlan:
        self._assert_trusted(manifest)
        component_root = self.components_root / manifest.name
        staging_root = component_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        self._assert_within_root(component_root)
        self._assert_within_root(staging_root)
        return InstallPlan(
            component=manifest,
            target_dir=component_root / manifest.version,
            staging_dir=staging_root,
            artifact_name=f"{manifest.name}-{manifest.version}",
            max_retries=self.max_retries,
        )

    def install(self, manifest: ComponentManifest) -> InstallResult:
        self._assert_trusted(manifest)
        plan = self.plan(manifest)
        component_root = self.components_root / manifest.name
        self._assert_within_root(component_root)
        component_root.mkdir(parents=True, exist_ok=True)
        previous = self._current_version(component_root)
        if previous is not None and previous["path"] == plan.target_dir:
            if self._validate_install_marker(plan.target_dir, manifest):
                return InstallResult(
                    component=manifest,
                    installed_path=plan.target_dir,
                    status="ALREADY_INSTALLED",
                    retry_count=0,
                )
        shutil.rmtree(plan.staging_dir, ignore_errors=True)
        plan.staging_dir.mkdir(parents=True, exist_ok=True)
        last_error: str | None = None
        attempts = 0
        for attempt in range(plan.max_retries):
            attempts = attempt + 1
            staging = plan.staging_dir / f"{uuid.uuid4().hex}"
            staging.mkdir(parents=True)
            self._assert_within_root(staging)
            try:
                archive_path = self._download(manifest, staging)
                extract_root = staging / "extract"
                extract_root.mkdir(parents=True)
                self._extract(manifest, archive_path, extract_root)
                source_root = self._archive_root(manifest, extract_root)
                if manifest.source_patch == "supervisor-runtime-v1":
                    apply_lcb_runtime_hardening(source_root)
                node_executable, npm_script = self._managed_toolchain()
                for command in manifest.install_commands:
                    exit_code = self._runner(
                        self._expand_command(command, node_executable, npm_script),
                        source_root,
                    )
                    if exit_code != 0:
                        raise RuntimeError(
                            f"install command failed with exit code {exit_code}"
                        )
                self._write_install_marker(source_root, manifest)
                promoted = self._promote(source_root, plan.target_dir)
                self._write_current(component_root, manifest, plan.target_dir)
                shutil.rmtree(plan.staging_dir, ignore_errors=True)
                return InstallResult(
                    component=manifest,
                    installed_path=promoted,
                    status="INSTALLED",
                    retry_count=attempts,
                )
            except Exception as exc:  # noqa: BLE001 - bounded retry/rollback
                last_error = f"{type(exc).__name__}: {exc}"
                shutil.rmtree(staging, ignore_errors=True)
                if attempt + 1 < plan.max_retries:
                    continue
                if previous is not None:
                    self._write_current(
                        component_root,
                        previous["manifest"],
                        previous["path"],
                    )
                    return InstallResult(
                        component=manifest,
                        installed_path=previous["path"],
                        status="ROLLED_BACK",
                        error=last_error,
                        retry_count=attempts,
                    )
                return InstallResult(
                    component=manifest,
                    status="FAILED",
                    error=last_error,
                    retry_count=attempts,
                )
        return InstallResult(
            component=manifest,
            status="FAILED",
            error=last_error,
            retry_count=attempts,
        )

    @staticmethod
    def _verify_checksum(payload: bytes, expected: str, name: str) -> None:
        digest = hashlib.sha256(payload).hexdigest()
        if digest.lower() != expected.strip().lower():
            raise RuntimeError(f"checksum mismatch for {name}")

    @staticmethod
    def _default_downloader(url: str, destination: Path) -> Path:
        return HttpsDownloader().download(url, destination)

    @staticmethod
    def _default_runner(command: list[str], cwd: Path) -> int:
        try:
            result = subprocess.run(
                list(command),
                cwd=str(cwd),
                shell=False,
                capture_output=True,
                text=True,
                check=False,
                timeout=INSTALL_COMMAND_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"install command timed out after {INSTALL_COMMAND_TIMEOUT_SECONDS:g} seconds"
            ) from exc
        return result.returncode

    def _current_version(self, component_root: Path) -> dict[str, object] | None:
        pointer = component_root / "current.json"
        if not pointer.exists():
            return None
        try:
            raw = json.loads(pointer.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        path = raw.get("path")
        if not isinstance(path, str) or not Path(path).is_dir():
            return None
        manifest = raw.get("manifest")
        if not isinstance(manifest, dict):
            return None
        return {"path": Path(path), "manifest": ComponentManifest.model_validate(manifest)}

    def _download(
        self,
        manifest: ComponentManifest,
        staging: Path,
    ) -> Path:
        target = staging / "download"
        payload = self._downloader(manifest.source, target)
        if isinstance(payload, bytes):
            if manifest.checksum_sha256:
                self._verify_checksum(payload, manifest.checksum_sha256, manifest.name)
            target.write_bytes(payload)
        else:
            if manifest.checksum_sha256:
                digest = hashlib.sha256(payload.read_bytes()).hexdigest()
                if digest.lower() != manifest.checksum_sha256.lower():
                    raise RuntimeError(f"checksum mismatch for {manifest.name}")
        if manifest.checksum_source:
            checksum_path = staging / "SHA256SUMS.txt"
            checksum_payload = self._downloader(manifest.checksum_source, checksum_path)
            if isinstance(checksum_payload, bytes):
                checksum_path.write_bytes(checksum_payload)
            try:
                checksum_text = checksum_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                raise RuntimeError("official checksum manifest could not be read") from exc
            expected_name = manifest.checksum_entry or Path(manifest.source).name
            expected_digest = _checksum_from_manifest(checksum_text, expected_name)
            actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if expected_digest.lower() != actual_digest.lower():
                raise RuntimeError(f"official checksum mismatch for {manifest.name}")
        return target

    def _extract(
        self,
        manifest: ComponentManifest,
        archive_path: Path,
        destination: Path,
    ) -> None:
        kind = manifest.archive_kind
        if kind == "auto":
            suffix = archive_path.suffix.lower()
            kind = "zip" if suffix == ".zip" else "tgz"
        if kind == "zip":
            extract_zip_safe(archive_path, destination)
            return
        if kind == "tgz":
            extract_tar_safe(archive_path, destination)
            return
        raise ValueError(f"unsupported archive kind: {manifest.archive_kind}")

    @staticmethod
    def _archive_root(manifest: ComponentManifest, extract_root: Path) -> Path:
        if manifest.archive_root:
            root = extract_root / manifest.archive_root
            if not root.is_dir():
                raise RuntimeError(
                    f"component {manifest.name} archive root is missing: {root}"
                )
            return root
        children = [path for path in extract_root.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            return children[0]
        if any(path.is_dir() for path in children):
            raise RuntimeError(
                f"component {manifest.name} archive has no single known root"
            )
        return extract_root

    def _promote(self, source: Path, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        old = target.parent / f".previous-{uuid.uuid4().hex}"
        if target.exists():
            os.replace(target, old)
        try:
            os.replace(source, target)
        except OSError:
            if old.exists() and not target.exists():
                os.replace(old, target)
            raise
        if old.exists():
            shutil.rmtree(old, ignore_errors=True)
        return target

    def _managed_toolchain(self) -> tuple[str | None, str | None]:
        node_root = self.components_root / "nodejs"
        current = self._current_version(node_root)
        if current is None:
            return None, None
        node_exe = Path(str(current["path"])) / "node.exe"
        npm_script = (
            Path(str(current["path"]))
            / "node_modules"
            / "npm"
            / "bin"
            / "npm-cli.js"
        )
        if not node_exe.is_file() or not npm_script.is_file():
            return None, None
        return str(node_exe), str(npm_script)

    @staticmethod
    def _expand_command(
        command: list[str],
        node_executable: str | None,
        npm_script: str | None,
    ) -> list[str]:
        if not command:
            return command
        launcher = command[0].lower()
        if launcher in {"node", "node.exe"} and node_executable:
            return [node_executable, *command[1:]]
        if launcher in {"npm", "npm.cmd", "npm.exe"}:
            if node_executable and npm_script:
                return [node_executable, npm_script, *command[1:]]
            if launcher == "npm":
                return ["npm", *command[1:]]
            return [launcher, *command[1:]]
        return list(command)

    def _write_install_marker(self, root: Path, manifest: ComponentManifest) -> Path:
        marker = root / ".codex-supervisor-installed.json"
        payload = {
            "name": manifest.name,
            "version": manifest.version,
            "source_ref": manifest.source_ref,
            "commit_sha": manifest.commit_sha,
            "checksum_sha256": manifest.checksum_sha256,
            "entrypoint": manifest.entrypoint,
            "source_patch": manifest.source_patch,
            "installed_at": datetime.now(timezone.utc).isoformat(),
        }
        marker.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return marker

    def _validate_install_marker(
        self,
        root: Path,
        manifest: ComponentManifest,
    ) -> bool:
        marker = root / ".codex-supervisor-installed.json"
        try:
            payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return False
        if (
            payload.get("name") != manifest.name
            or payload.get("version") != manifest.version
            or payload.get("source_ref") != manifest.source_ref
            or payload.get("commit_sha") != manifest.commit_sha
            or payload.get("source_patch") != manifest.source_patch
        ):
            return False
        if manifest.entrypoint and not (root / manifest.entrypoint).is_file():
            return False
        if (
            manifest.source_patch == "supervisor-runtime-v1"
            and not has_lcb_runtime_hardening(root)
        ):
            return False
        if manifest.entrypoint and manifest.version_args:
            executable = root / manifest.entrypoint
            try:
                result = subprocess.run(
                    [str(executable), *manifest.version_args],
                    cwd=str(root),
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=15.0,
                )
            except (OSError, subprocess.TimeoutExpired):
                return False
            if result.returncode != 0:
                return False
            if manifest.version_contains:
                output = f"{result.stdout}\n{result.stderr}"
                if manifest.version_contains not in output:
                    return False
        return True

    @staticmethod
    def _write_current(
        component_root: Path,
        manifest: ComponentManifest,
        target_dir: Path,
    ) -> Path:
        pointer = component_root / "current.json"
        payload = {
            "name": manifest.name,
            "version": manifest.version,
            "path": str(target_dir),
            "manifest": manifest.model_dump(mode="json"),
        }
        fd, temporary = tempfile.mkstemp(
            prefix="current-",
            suffix=".tmp",
            dir=component_root,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, pointer)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return pointer

    def _assert_trusted(self, manifest: ComponentManifest) -> None:
        if not self._trusted:
            return
        trusted = self._trusted.get(manifest.name)
        if trusted is None or trusted != manifest:
            raise ValueError(
                f"component {manifest.name!r} is not from the Bridge trusted registry"
            )

    def _assert_within_root(self, path: Path) -> None:
        resolved = path.resolve()
        root = self.components_root.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(
                f"component path escapes the managed components root: {resolved}"
            )


def _checksum_from_manifest(payload: str, artifact_name: str) -> str:
    """Read one exact artifact digest from an upstream SHA256SUMS file."""

    normalized = Path(artifact_name).name
    for line in payload.splitlines():
        parts = line.strip().split()
        if len(parts) < 2:
            continue
        digest, name = parts[0], parts[-1].lstrip("*")
        if name == normalized and len(digest) == 64 and all(
            char in "0123456789abcdefABCDEF" for char in digest
        ):
            return digest
    raise RuntimeError(f"artifact {normalized} is missing from official checksum manifest")
