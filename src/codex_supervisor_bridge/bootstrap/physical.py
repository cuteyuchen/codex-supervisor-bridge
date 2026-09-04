from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, Protocol

SUPERVISOR_HOST_PATH_VIRTUALIZED = "SUPERVISOR_HOST_PATH_VIRTUALIZED"
SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH = "SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH"
SUPERVISOR_COMPONENT_ROOT_MISMATCH = "SUPERVISOR_COMPONENT_ROOT_MISMATCH"
SUPERVISOR_RUNTIME_ROOT_MISMATCH = "SUPERVISOR_RUNTIME_ROOT_MISMATCH"
LCB_PHYSICAL_ROOT_MISMATCH = "LCB_PHYSICAL_ROOT_MISMATCH"
CODEX_HOME_PHYSICAL_ROOT_MISMATCH = "CODEX_HOME_PHYSICAL_ROOT_MISMATCH"
PHYSICAL_PATH_UNVERIFIED = "PHYSICAL_PATH_UNVERIFIED"


@dataclass(frozen=True)
class PhysicalPathEvidence:
    """Bounded evidence for one requested path and its physical file view."""

    requested_path: str
    physical_path: str | None = None
    exists: bool = False
    is_directory: bool | None = None
    is_reparse_point: bool = False
    volume_identity: str | None = None
    file_identity: dict[str, int] | None = None
    nearest_existing_path: str | None = None
    nearest_existing_physical_path: str | None = None
    verified: bool = False
    failure_code: str | None = None
    technical_detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_path": self.requested_path,
            "physical_path": self.physical_path,
            "exists": self.exists,
            "is_directory": self.is_directory,
            "is_reparse_point": self.is_reparse_point,
            "volume_identity": self.volume_identity,
            "file_identity": dict(self.file_identity) if self.file_identity else None,
            "nearest_existing_path": self.nearest_existing_path,
            "nearest_existing_physical_path": self.nearest_existing_physical_path,
            "verified": self.verified,
            "failure_code": self.failure_code,
            "technical_detail": self.technical_detail,
        }


class PhysicalPathInspector(Protocol):
    def inspect(self, path: str | Path) -> PhysicalPathEvidence: ...


class PhysicalPathVerificationError(RuntimeError):
    """Raised when a path cannot be proven to be in the intended namespace."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        evidence: PhysicalPathEvidence | None = None,
    ) -> None:
        self.code = code
        self.evidence = evidence
        super().__init__(f"{code}: {message}")


class WindowsPhysicalPathInspector:
    """Resolve Windows paths through a real handle, with a portable fallback."""

    def __init__(self, *, windows: bool | None = None) -> None:
        self.windows = platform.system() == "Windows" if windows is None else windows

    def inspect(self, path: str | Path) -> PhysicalPathEvidence:
        requested = Path(path).expanduser()
        if not requested.is_absolute():
            requested = requested.absolute()
        if self.windows:
            return self._inspect_windows(requested)
        return self._inspect_portable(requested)

    def _inspect_portable(self, requested: Path) -> PhysicalPathEvidence:
        requested_text = str(requested)
        try:
            resolved = requested.resolve(strict=True)
            stat = resolved.stat()
        except (OSError, RuntimeError):
            parent = _nearest_existing(requested)
            if parent is None:
                return PhysicalPathEvidence(requested_path=requested_text)
            try:
                parent_resolved = parent.resolve(strict=True)
            except (OSError, RuntimeError):
                return PhysicalPathEvidence(
                    requested_path=requested_text,
                    nearest_existing_path=str(parent),
                )
            return PhysicalPathEvidence(
                requested_path=requested_text,
                nearest_existing_path=str(parent),
                nearest_existing_physical_path=str(parent_resolved),
            )
        return PhysicalPathEvidence(
            requested_path=requested_text,
            physical_path=str(resolved),
            exists=True,
            is_directory=resolved.is_dir(),
            is_reparse_point=requested.is_symlink(),
            volume_identity=str(stat.st_dev),
            file_identity={"device": int(stat.st_dev), "inode": int(stat.st_ino)},
            nearest_existing_path=str(requested),
            nearest_existing_physical_path=str(resolved),
        )

    def _inspect_windows(self, requested: Path) -> PhysicalPathEvidence:
        try:
            return _inspect_windows_handle(requested)
        except (OSError, AttributeError, ctypes.ArgumentError):
            parent = _nearest_existing(requested)
            if parent is None:
                return PhysicalPathEvidence(requested_path=str(requested))
            try:
                parent_evidence = _inspect_windows_handle(parent)
            except (OSError, AttributeError, ctypes.ArgumentError):
                return PhysicalPathEvidence(
                    requested_path=str(requested),
                    nearest_existing_path=str(parent),
                )
            return PhysicalPathEvidence(
                requested_path=str(requested),
                nearest_existing_path=str(parent),
                nearest_existing_physical_path=parent_evidence.physical_path,
            )


class PhysicalPathGuard:
    """Fail-closed path policy used before every managed write or spawn."""

    def __init__(
        self,
        inspector: PhysicalPathInspector | None = None,
        *,
        allow_packaged_legacy: bool = False,
    ) -> None:
        self.inspector = inspector or WindowsPhysicalPathInspector()
        self.allow_packaged_legacy = allow_packaged_legacy

    def inspect(self, path: str | Path) -> PhysicalPathEvidence:
        return self.inspector.inspect(path)

    def for_legacy_migration(self) -> "PhysicalPathGuard":
        """Return a narrowly scoped guard for an explicit Python-root migration."""

        return PhysicalPathGuard(self.inspector, allow_packaged_legacy=True)

    def verify_root(
        self,
        path: str | Path,
        *,
        role: str = "path",
        require_directory: bool = False,
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        evidence = self.inspect(path)
        failure = self._root_failure(
            evidence,
            role=role,
            require_directory=require_directory,
            allow_packaged_legacy=self._allow_packaged_legacy(allow_packaged_legacy),
        )
        if failure is not None:
            raise failure
        return replace(evidence, verified=True)

    def verify_subpath(
        self,
        path: str | Path,
        root: str | Path,
        *,
        role: str = "path",
        require_directory: bool = False,
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        allow_legacy = self._allow_packaged_legacy(allow_packaged_legacy)
        root_evidence = self.verify_root(
            root,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_legacy,
        )
        evidence = self.inspect(path)
        failure = self._root_failure(
            evidence,
            role=role,
            require_directory=require_directory,
            allow_packaged_legacy=allow_legacy,
        )
        if failure is not None:
            raise failure
        physical = evidence.physical_path or evidence.nearest_existing_physical_path
        physical_root = root_evidence.physical_path or root_evidence.nearest_existing_physical_path
        if physical is None or physical_root is None or not _is_within(physical, physical_root):
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "path is outside the verified physical root",
                evidence=evidence,
            )
        if (
            root_evidence.volume_identity is not None
            and evidence.volume_identity is not None
            and root_evidence.volume_identity != evidence.volume_identity
        ):
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "path resolves on a different physical volume",
                evidence=evidence,
            )
        return replace(evidence, verified=True)

    def before_write(
        self,
        path: str | Path,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        target = Path(path).expanduser()
        if target.exists():
            return self.verify_root(
                target,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        parent = _nearest_existing(target.parent)
        if parent is None:
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "no existing parent can be verified before write",
            )
        parent_evidence = self.verify_root(
            parent,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        # Validate the requested lexical namespace as well as its current
        # parent. This catches a packaged LocalCache target before creation.
        self._assert_requested_namespace(
            target,
            role=role,
            allow_packaged_legacy=self._allow_packaged_legacy(allow_packaged_legacy),
        )
        return replace(
            PhysicalPathEvidence(
                requested_path=str(target),
                nearest_existing_path=str(parent),
                nearest_existing_physical_path=(
                    parent_evidence.physical_path
                    or parent_evidence.nearest_existing_physical_path
                ),
            ),
            verified=True,
        )

    def before_delete(
        self,
        path: str | Path,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        target = Path(path).expanduser()
        evidence = self.verify_root(
            target,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        self.verify_root(
            target.parent,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        return evidence

    def ensure_directory(
        self,
        path: str | Path,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        allow_legacy = self._allow_packaged_legacy(allow_packaged_legacy)
        target = Path(path).expanduser()
        if target.exists():
            return self.verify_root(
                target,
                role=role,
                require_directory=True,
                allow_packaged_legacy=allow_legacy,
            )
        missing: list[Path] = []
        current: Path | None = target
        while current is not None and not current.exists():
            missing.append(current)
            parent = current.parent
            if parent == current:
                current = None
                break
            current = parent
        if current is None:
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "no existing parent can be verified before directory creation",
            )
        self.verify_root(
            current,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_legacy,
        )
        for child in reversed(missing):
            self.before_write(
                child,
                role=role,
                allow_packaged_legacy=allow_legacy,
            )
            try:
                child.mkdir(exist_ok=False)
            except FileExistsError:
                pass
            except OSError as exc:
                raise PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    f"directory creation failed: {type(exc).__name__}",
                ) from exc
            evidence = self.verify_root(
                child,
                role=role,
                require_directory=True,
                allow_packaged_legacy=allow_legacy,
            )
            current = child
        return evidence

    def before_spawn(
        self,
        command: list[str] | tuple[str, ...],
        *,
        cwd: str | Path | None = None,
        role: str = "process",
        allow_packaged_legacy: bool | None = None,
    ) -> None:
        if cwd is not None:
            self.verify_root(
                cwd,
                role=role,
                require_directory=True,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        if not command:
            raise PhysicalPathVerificationError(
                PHYSICAL_PATH_UNVERIFIED,
                "process command is empty",
            )
        base_directory = Path(cwd).expanduser() if cwd is not None else Path.cwd()
        for index, raw_argument in enumerate(command):
            argument = str(raw_argument)
            if not argument or argument.startswith("-") or "://" in argument:
                continue
            candidate = Path(argument).expanduser()
            explicit_path = candidate.is_absolute() or _looks_like_path(argument)
            if not explicit_path:
                continue
            if not candidate.is_absolute():
                candidate = base_directory / candidate
            if index == 0 or candidate.exists():
                self.verify_root(
                    candidate,
                    role=role,
                    allow_packaged_legacy=allow_packaged_legacy,
                )

    def create_temp_file(
        self,
        directory: str | Path,
        *,
        prefix: str = "tmp-",
        suffix: str = ".tmp",
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> tuple[int, Path]:
        """Create a temp file below a verified directory and recheck its handle path."""

        directory_path = Path(directory).expanduser()
        self.ensure_directory(
            directory_path,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        descriptor, raw_path = tempfile.mkstemp(
            prefix=prefix,
            suffix=suffix,
            dir=str(directory_path),
        )
        path = Path(raw_path)
        try:
            self.verify_root(
                path,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        except Exception:
            os.close(descriptor)
            raise
        return descriptor, path

    def write_text(
        self,
        path: str | Path,
        content: str,
        *,
        role: str = "path",
        encoding: str = "utf-8",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Atomically write text through a verified temporary file."""

        target = Path(path).expanduser()
        self.ensure_directory(
            target.parent,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        self.before_write(
            target,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        descriptor, temporary = self.create_temp_file(
            target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        try:
            with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return self.replace(
                temporary,
                target,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        finally:
            self.remove(
                temporary,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )

    def write_bytes(
        self,
        path: str | Path,
        content: bytes,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Atomically write bytes through a verified temporary file."""

        target = Path(path).expanduser()
        self.ensure_directory(
            target.parent,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        self.before_write(
            target,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        descriptor, temporary = self.create_temp_file(
            target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            return self.replace(
                temporary,
                target,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        finally:
            self.remove(
                temporary,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )

    def write_stream(
        self,
        path: str | Path,
        chunks: Iterable[bytes],
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Atomically write a binary stream without reopening the target path."""

        target = Path(path).expanduser()
        self.ensure_directory(
            target.parent,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        self.before_write(
            target,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        descriptor, temporary = self.create_temp_file(
            target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                _write_chunks(handle, chunks)
                handle.flush()
                os.fsync(handle.fileno())
            return self.replace(
                temporary,
                target,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )
        finally:
            self.remove(
                temporary,
                role=role,
                allow_packaged_legacy=allow_packaged_legacy,
            )

    def copy_file(
        self,
        source: str | Path,
        target: str | Path,
        *,
        source_guard: "PhysicalPathGuard | None" = None,
        source_role: str = "path",
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Copy a verified regular file through a guarded destination handle."""

        source_path = Path(source).expanduser()
        source_policy = source_guard or self
        source_allow_legacy = (
            source_guard.allow_packaged_legacy
            if source_guard is not None
            else allow_packaged_legacy
        )
        source_evidence = source_policy.verify_root(
            source_path,
            role=source_role,
            allow_packaged_legacy=source_allow_legacy,
        )
        if not source_evidence.exists or source_evidence.is_directory is True:
            raise PhysicalPathVerificationError(
                self._failure_code(source_role, virtualized=False),
                "copy source is not a regular file",
                evidence=source_evidence,
            )
        try:
            with source_path.open("rb") as handle:
                return self.write_stream(
                    target,
                    _read_chunks(handle),
                    role=role,
                    allow_packaged_legacy=allow_packaged_legacy,
                )
        except OSError as exc:
            raise PhysicalPathVerificationError(
                self._failure_code(source_role, virtualized=False),
                f"copy source could not be read: {type(exc).__name__}",
                evidence=source_evidence,
            ) from exc

    def copy_tree(
        self,
        source: str | Path,
        target: str | Path,
        *,
        source_guard: "PhysicalPathGuard | None" = None,
        source_role: str = "path",
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> None:
        """Copy a verified directory tree without following links or reparse points."""

        source_path = Path(source).expanduser()
        target_path = Path(target).expanduser()
        source_policy = source_guard or self
        source_allow_legacy = (
            source_guard.allow_packaged_legacy
            if source_guard is not None
            else allow_packaged_legacy
        )
        source_policy.verify_tree(
            source_path,
            role=source_role,
            allow_packaged_legacy=source_allow_legacy,
        )
        if target_path.exists() or target_path.is_symlink():
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "copy destination already exists",
            )
        self.ensure_directory(target_path.parent, role=role)
        self.before_write(target_path, role=role)
        target_path.mkdir()
        self.verify_root(target_path, role=role, require_directory=True)
        for child in source_path.iterdir():
            destination = target_path / child.name
            evidence = source_policy.verify_root(
                child,
                role=source_role,
                allow_packaged_legacy=source_allow_legacy,
            )
            if evidence.is_directory:
                self.copy_tree(
                    child,
                    destination,
                    source_guard=source_guard,
                    source_role=source_role,
                    role=role,
                    allow_packaged_legacy=source_allow_legacy,
                )
            else:
                self.copy_file(
                    child,
                    destination,
                    source_guard=source_guard,
                    source_role=source_role,
                    role=role,
                    allow_packaged_legacy=source_allow_legacy,
                )
        self.verify_tree(target_path, role=role)

    def replace(
        self,
        source: str | Path,
        target: str | Path,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Atomically replace a managed path only after both sides are checked."""

        self.verify_root(source, role=role, allow_packaged_legacy=allow_packaged_legacy)
        self.before_write(target, role=role, allow_packaged_legacy=allow_packaged_legacy)
        os.replace(source, target)
        return self.verify_root(target, role=role, allow_packaged_legacy=allow_packaged_legacy)

    def remove(
        self,
        path: str | Path,
        *,
        role: str = "path",
        recursive: bool = False,
        allow_packaged_legacy: bool | None = None,
    ) -> bool:
        """Remove one verified file/tree without following reparse points."""

        target = Path(path).expanduser()
        if not target.exists() and not target.is_symlink():
            return False
        evidence = self.before_delete(
            target,
            role=role,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        if recursive:
            if evidence.is_directory is not True:
                raise PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    "recursive removal target is not a directory",
                    evidence=evidence,
                )
            self._verify_tree(
                target,
                role=role,
                allow_packaged_legacy=self._allow_packaged_legacy(allow_packaged_legacy),
            )
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        return True

    def verify_tree(
        self,
        path: str | Path,
        *,
        role: str = "path",
        allow_packaged_legacy: bool | None = None,
    ) -> PhysicalPathEvidence:
        """Validate every member of a directory before it is copied or removed."""

        target = Path(path).expanduser()
        allow_legacy = self._allow_packaged_legacy(allow_packaged_legacy)
        evidence = self.verify_root(
            target,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_legacy,
        )
        self._verify_tree(
            target,
            role=role,
            allow_packaged_legacy=allow_legacy,
        )
        return evidence

    def assert_roots(self, roots: Mapping[str, str | Path]) -> dict[str, PhysicalPathEvidence]:
        evidence: dict[str, PhysicalPathEvidence] = {}
        for role, path in roots.items():
            evidence[role] = self.verify_root(path, role=role)
        return evidence

    @staticmethod
    def _failure_code(
        role: str,
        *,
        virtualized: bool,
    ) -> str:
        if role == "app_data":
            return SUPERVISOR_HOST_PATH_VIRTUALIZED if virtualized else SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH
        if role == "components":
            return SUPERVISOR_COMPONENT_ROOT_MISMATCH
        if role == "runtime":
            return SUPERVISOR_RUNTIME_ROOT_MISMATCH
        if role == "lcb":
            return LCB_PHYSICAL_ROOT_MISMATCH
        if role == "codex_home":
            return CODEX_HOME_PHYSICAL_ROOT_MISMATCH
        return PHYSICAL_PATH_UNVERIFIED

    def _root_failure(
        self,
        evidence: PhysicalPathEvidence,
        *,
        role: str,
        require_directory: bool,
        allow_packaged_legacy: bool,
    ) -> PhysicalPathVerificationError | None:
        physical = evidence.physical_path
        if physical is None:
            physical = evidence.nearest_existing_physical_path
        if physical is None:
            return PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "physical path could not be resolved",
                evidence=evidence,
            )
        if evidence.is_reparse_point:
            return PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "path resolves through a reparse point",
                evidence=evidence,
            )
        requested_packaged = _is_packaged_bridge_path(evidence.requested_path)
        physical_packaged = _is_packaged_bridge_path(physical)
        if requested_packaged or physical_packaged:
            requested_legacy = _is_python_legacy_packaged_path(evidence.requested_path)
            physical_legacy = _is_python_legacy_packaged_path(physical)
            if (
                not allow_packaged_legacy
                or (requested_packaged and not requested_legacy)
                or (physical_packaged and not physical_legacy)
            ):
                return PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=True),
                    "path resolves into a packaged LocalCache view",
                    evidence=evidence,
                )
            if require_directory and evidence.exists and evidence.is_directory is not True:
                return PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    "verified path is not a directory",
                    evidence=evidence,
                )
            return None
        if evidence.exists:
            if not _same_path(evidence.requested_path, physical):
                return PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    "requested path and physical path differ",
                    evidence=evidence,
                )
            if require_directory and evidence.is_directory is not True:
                return PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    "verified path is not a directory",
                    evidence=evidence,
                )
            return None
        parent_requested = evidence.nearest_existing_path
        if parent_requested is None:
            return PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "nearest existing parent is unavailable",
                evidence=evidence,
            )
        if not _same_path(parent_requested, physical):
            return PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "nearest existing parent is redirected",
                evidence=evidence,
            )
        return None

    def _verify_tree(
        self,
        root: Path,
        *,
        role: str,
        allow_packaged_legacy: bool,
    ) -> None:
        root_evidence = self.verify_root(
            root,
            role=role,
            require_directory=True,
            allow_packaged_legacy=allow_packaged_legacy,
        )
        physical_root = root_evidence.physical_path or root_evidence.nearest_existing_physical_path
        if physical_root is None:
            raise PhysicalPathVerificationError(
                self._failure_code(role, virtualized=False),
                "tree physical root could not be resolved",
                evidence=root_evidence,
            )
        pending = [root]
        while pending:
            current = pending.pop()
            try:
                children = list(current.iterdir())
            except OSError as exc:
                raise PhysicalPathVerificationError(
                    self._failure_code(role, virtualized=False),
                    f"tree inspection failed: {type(exc).__name__}",
                ) from exc
            for child in children:
                evidence = self.verify_root(
                    child,
                    role=role,
                    allow_packaged_legacy=allow_packaged_legacy,
                )
                physical = evidence.physical_path or evidence.nearest_existing_physical_path
                if physical is None or not _is_within(physical, physical_root):
                    raise PhysicalPathVerificationError(
                        self._failure_code(role, virtualized=False),
                        "tree member is outside the verified physical root",
                        evidence=evidence,
                    )
                if evidence.is_directory:
                    pending.append(child)

    def _allow_packaged_legacy(self, override: bool | None) -> bool:
        return self.allow_packaged_legacy if override is None else override

    def _assert_requested_namespace(
        self,
        path: Path,
        *,
        role: str,
        allow_packaged_legacy: bool,
    ) -> None:
        if not _is_packaged_bridge_path(str(path)):
            return
        if allow_packaged_legacy and _is_python_legacy_packaged_path(str(path)):
            return
        raise PhysicalPathVerificationError(
            self._failure_code(role, virtualized=True),
            "requested path is a packaged LocalCache view",
        )


def _write_chunks(handle: BinaryIO, chunks: Iterable[bytes]) -> None:
    for chunk in chunks:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("guarded binary writes require bytes-like chunks")
        handle.write(chunk)


def _read_chunks(handle: BinaryIO, *, chunk_size: int = 1024 * 1024) -> Iterable[bytes]:
    while True:
        chunk = handle.read(chunk_size)
        if not chunk:
            return
        yield chunk


def current_package_identity() -> str:
    """Return only the package identity category, never package credentials."""

    if platform.system() != "Windows":
        return "NO_PACKAGE_IDENTITY"
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentPackageFullName.argtypes = [
            ctypes.POINTER(ctypes.wintypes.UINT),
            ctypes.wintypes.LPWSTR,
        ]
        kernel32.GetCurrentPackageFullName.restype = ctypes.wintypes.LONG
        length = ctypes.wintypes.UINT(0)
        status = kernel32.GetCurrentPackageFullName(ctypes.byref(length), None)
        if status == 15700:  # APPMODEL_ERROR_NO_PACKAGE
            return "NO_PACKAGE_IDENTITY"
        if status not in {0, 122} or length.value <= 0:
            return "UNKNOWN"
        buffer = ctypes.create_unicode_buffer(length.value)
        status = kernel32.GetCurrentPackageFullName(ctypes.byref(length), buffer)
        return buffer.value if status == 0 and buffer.value else "UNKNOWN"
    except (AttributeError, OSError, ctypes.ArgumentError):
        return "UNKNOWN"


def _inspect_windows_handle(requested: Path) -> PhysicalPathEvidence:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFinalPathNameByHandleW.argtypes = [
        wintypes.HANDLE,
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
    ]
    kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL

    FILE_READ_ATTRIBUTES = 0x0080
    FILE_SHARE_READ = 0x00000001
    FILE_SHARE_WRITE = 0x00000002
    FILE_SHARE_DELETE = 0x00000004
    OPEN_EXISTING = 3
    FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
    FILE_ATTRIBUTE_DIRECTORY = 0x00000010
    FILE_NAME_NORMALIZED = 0x0
    invalid_handle = ctypes.c_void_p(-1).value

    handle = kernel32.CreateFileW(
        str(requested),
        FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    handle_value = getattr(handle, "value", handle)
    if handle_value in {None, 0, -1, invalid_handle}:
        raise OSError(ctypes.get_last_error(), f"CreateFileW failed for {requested}")
    try:
        length = 512
        while True:
            buffer = ctypes.create_unicode_buffer(length)
            written = kernel32.GetFinalPathNameByHandleW(
                handle,
                buffer,
                length,
                FILE_NAME_NORMALIZED,
            )
            if written == 0:
                raise OSError(ctypes.get_last_error(), "GetFinalPathNameByHandleW failed")
            if written < length - 1:
                break
            length *= 2

        class _FileTime(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        class _FileInfo(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wintypes.DWORD),
                ("ftCreationTime", _FileTime),
                ("ftLastAccessTime", _FileTime),
                ("ftLastWriteTime", _FileTime),
                ("dwVolumeSerialNumber", wintypes.DWORD),
                ("nFileSizeHigh", wintypes.DWORD),
                ("nFileSizeLow", wintypes.DWORD),
                ("nNumberOfLinks", wintypes.DWORD),
                ("nFileIndexHigh", wintypes.DWORD),
                ("nFileIndexLow", wintypes.DWORD),
            ]

        info = _FileInfo()
        if not kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), "GetFileInformationByHandle failed")
        physical = _strip_extended_prefix(buffer.value)
        try:
            stat_attributes = int(getattr(os.lstat(requested), "st_file_attributes", 0))
        except OSError:
            stat_attributes = 0
        return PhysicalPathEvidence(
            requested_path=str(requested),
            physical_path=physical,
            exists=True,
            is_directory=bool(info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY),
            is_reparse_point=bool(
                info.dwFileAttributes & 0x00000400
                or stat_attributes & 0x00000400
            ),
            volume_identity=f"{int(info.dwVolumeSerialNumber):08x}",
            file_identity={
                "volume_serial": int(info.dwVolumeSerialNumber),
                "file_index_high": int(info.nFileIndexHigh),
                "file_index_low": int(info.nFileIndexLow),
            },
            nearest_existing_path=str(requested),
            nearest_existing_physical_path=physical,
        )
    finally:
        kernel32.CloseHandle(handle)


def _nearest_existing(path: Path) -> Path | None:
    current = path
    while True:
        try:
            if current.exists():
                return current
        except OSError:
            pass
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _same_path(left: str | Path, right: str | Path) -> bool:
    return _path_key(left) == _path_key(right)


def _is_within(path: str | Path, root: str | Path) -> bool:
    left = _path_key(path).rstrip("\\/")
    right = _path_key(root).rstrip("\\/")
    return left == right or left.startswith(right + "\\") or left.startswith(right + "/")


def _path_key(value: str | Path) -> str:
    text = str(value).replace("/", "\\")
    text = _strip_extended_prefix(text)
    return os.path.normcase(os.path.normpath(text)).casefold()


def _strip_extended_prefix(value: str) -> str:
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def _is_packaged_bridge_path(value: str) -> bool:
    parts = [part.casefold() for part in value.replace("/", "\\").split("\\") if part]
    for index in range(len(parts)):
        if (
            parts[index : index + 3] == ["appdata", "local", "packages"]
            and index + 6 < len(parts)
            and parts[index + 4 : index + 6] == ["localcache", "local"]
            and parts[index + 6] == "codexsupervisorbridge"
        ):
            return True
    return False


def _is_python_legacy_packaged_path(value: str) -> bool:
    parts = [part.casefold() for part in value.replace("/", "\\").split("\\") if part]
    for index in range(len(parts)):
        if parts[index : index + 3] != ["appdata", "local", "packages"]:
            continue
        if index + 6 >= len(parts):
            continue
        package = parts[index + 3]
        if (
            package.startswith("pythonsoftwarefoundation.python.")
            or package.startswith("python.")
        ) and parts[index + 4 : index + 7] == ["localcache", "local", "codexsupervisorbridge"]:
            return True
    return False


def _looks_like_path(value: str) -> bool:
    return "\\" in value or "/" in value or (len(value) > 1 and value[1] == ":")


__all__ = [
    "CODEX_HOME_PHYSICAL_ROOT_MISMATCH",
    "LCB_PHYSICAL_ROOT_MISMATCH",
    "PHYSICAL_PATH_UNVERIFIED",
    "PhysicalPathEvidence",
    "PhysicalPathGuard",
    "PhysicalPathInspector",
    "PhysicalPathVerificationError",
    "SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH",
    "SUPERVISOR_COMPONENT_ROOT_MISMATCH",
    "SUPERVISOR_HOST_PATH_VIRTUALIZED",
    "SUPERVISOR_RUNTIME_ROOT_MISMATCH",
    "WindowsPhysicalPathInspector",
    "current_package_identity",
]
