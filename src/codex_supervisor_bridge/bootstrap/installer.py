from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from pydantic import BaseModel, Field


class ComponentManifest(BaseModel):
    """Pinned, user-safe component descriptor managed by the Bridge."""

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    display_name: str
    version: str
    source: str
    source_ref: str
    checksum_sha256: str | None = None
    install_commands: list[str] = Field(default_factory=list)
    requires_node: bool = False


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
    downloader: Callable[[str, Path], bytes] | None = None
    runner: Callable[[list[str], Path], int] | None = None
    max_retries: int = 3


class ComponentInstaller:
    """App-managed dependency installer with atomic promotion and rollback."""

    def __init__(
        self,
        components_root: str | Path,
        *,
        downloader: Callable[[str, Path], bytes] | None = None,
        runner: Callable[[list[str], Path], int] | None = None,
        max_retries: int = 3,
    ) -> None:
        self.components_root = Path(components_root)
        self.components_root.mkdir(parents=True, exist_ok=True)
        self._downloader = downloader or self._default_downloader
        self._runner = runner or self._default_runner
        self.max_retries = max_retries

    def plan(self, manifest: ComponentManifest) -> InstallPlan:
        component_root = self.components_root / manifest.name
        staging_root = component_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        return InstallPlan(
            component=manifest,
            target_dir=component_root / manifest.version,
            staging_dir=staging_root,
            artifact_name=f"{manifest.name}-{manifest.version}",
            max_retries=self.max_retries,
        )

    def install(self, manifest: ComponentManifest) -> InstallResult:
        plan = self.plan(manifest)
        component_root = self.components_root / manifest.name
        component_root.mkdir(parents=True, exist_ok=True)
        previous = self._current_version(component_root)
        last_error: str | None = None
        attempts = 0
        for attempt in range(plan.max_retries):
            attempts = attempt + 1
            staging = plan.staging_dir / f"{uuid.uuid4().hex}"
            staging.mkdir(parents=True)
            try:
                artifact = staging / plan.artifact_name
                artifact.mkdir(parents=True)
                payload = self._downloader(manifest.source, artifact)
                if manifest.checksum_sha256:
                    self._verify_checksum(payload, manifest.checksum_sha256, manifest.name)
                for command in manifest.install_commands:
                    exit_code = self._runner(command, artifact)
                    if exit_code != 0:
                        raise RuntimeError(
                            f"install command failed with exit code {exit_code}: {command}"
                        )
                os.replace(artifact, plan.target_dir)
                self._write_current(component_root, manifest, plan.target_dir)
                shutil.rmtree(plan.staging_dir, ignore_errors=True)
                return InstallResult(
                    component=manifest,
                    installed_path=plan.target_dir,
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
    def _default_downloader(url: str, destination: Path) -> bytes:
        raise NotImplementedError(
            "network downloads are delegated to the Windows Gate installer"
        )

    @staticmethod
    def _default_runner(command: str, cwd: Path) -> int:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
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
