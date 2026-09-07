from __future__ import annotations

import re
import tarfile
import zipfile
from pathlib import Path

from .physical import PhysicalPathGuard


class UnsafeArchiveError(ValueError):
    """Raised when an archive tries to escape its extraction root."""


def extract_zip_safe(
    archive_path: Path,
    destination: Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> None:
    """Extract a ZIP archive without path traversal, drive escapes, or links."""
    guard = path_guard or PhysicalPathGuard()
    guard.verify_root(archive_path, role="components")
    guard.ensure_directory(destination, role="components")
    with zipfile.ZipFile(archive_path) as archive:
        for member in archive.infolist():
            _validate_zip_member(member)
            target = _resolve_member(destination, member.filename)
            if _is_zip_dir(member):
                guard.ensure_directory(target, role="components")
                continue
            if _is_zip_symlink(member):
                raise UnsafeArchiveError(
                    f"archive member is a symlink: {member.filename}"
                )
            guard.ensure_directory(target.parent, role="components")
            with archive.open(member) as source:
                guard.write_stream(
                    target,
                    iter(lambda: source.read(1024 * 1024), b""),
                    role="components",
                )


def extract_tar_safe(
    archive_path: Path,
    destination: Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> None:
    """Extract a tar archive without path traversal, links, or device nodes."""
    guard = path_guard or PhysicalPathGuard()
    guard.verify_root(archive_path, role="components")
    guard.ensure_directory(destination, role="components")
    with tarfile.open(archive_path, mode="r:*") as archive:
        for member in archive:
            if member.issym() or member.islnk():
                raise UnsafeArchiveError(
                    f"archive member is a link: {member.name}"
                )
            if member.isdev():
                raise UnsafeArchiveError(
                    f"archive member is a device node: {member.name}"
                )
            target = _resolve_member(destination, member.name)
            if member.isdir():
                guard.ensure_directory(target, role="components")
                continue
            if not member.isreg():
                continue
            guard.ensure_directory(target.parent, role="components")
            guard.before_write(target, role="components")
            source = archive.extractfile(member)
            if source is None:
                continue
            with source:
                guard.write_stream(
                    target,
                    iter(lambda: source.read(1024 * 1024), b""),
                    role="components",
                )


def _validate_zip_member(member: zipfile.ZipInfo) -> None:
    if _is_zip_symlink(member):
        raise UnsafeArchiveError(f"archive member is a symlink: {member.filename}")
    if _is_zip_dir(member):
        return
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    if unix_mode:
        kind = unix_mode & 0o170000
        if kind and kind not in {0o040000, 0o100000}:
            raise UnsafeArchiveError(
                f"archive member is not a regular file or directory: {member.filename}"
            )


def _is_zip_dir(member: zipfile.ZipInfo) -> bool:
    if member.filename.endswith("/"):
        return True
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    return bool(unix_mode) and (unix_mode & 0o170000) == 0o040000


def _is_zip_symlink(member: zipfile.ZipInfo) -> bool:
    unix_mode = (member.external_attr >> 16) & 0xFFFF
    return bool(unix_mode) and (unix_mode & 0o170000) == 0o120000


def _resolve_member(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    if "\x00" in normalized or normalized.startswith("//"):
        raise UnsafeArchiveError(f"archive member has an unsafe name: {name!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise UnsafeArchiveError(f"archive member has a drive path: {name}")
    path = Path(normalized)
    if path.is_absolute() or normalized.startswith("/"):
        raise UnsafeArchiveError(f"archive member is absolute: {name}")
    if any(part in {"..", ""} for part in path.parts):
        if ".." in path.parts:
            raise UnsafeArchiveError(f"archive member escapes the root: {name}")
    drive = path.drive or path.root
    if drive:
        raise UnsafeArchiveError(f"archive member has a drive path: {name}")
    resolved = (root / path).resolve()
    root_resolved = root.resolve()
    if resolved != root_resolved and root_resolved not in resolved.parents:
        raise UnsafeArchiveError(f"archive member escapes the root: {name}")
    return resolved
