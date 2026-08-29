from __future__ import annotations

import json
import os
import platform
import shutil
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

PERSISTENT_STATE_NAMES = ("database", "settings", "secrets", "runtime")
STATE_PATHS = {
    "database": Path("data") / "supervisor.db",
    "settings": Path("config") / "settings.json",
    "secrets": Path("config") / "secrets",
    "runtime": Path("runtime"),
    "components": Path("components"),
    "logs": Path("logs"),
    "cache": Path("cache"),
}
LEGACY_INACTIVE_MARKER = ".codex-supervisor-legacy-inactive.json"


@dataclass(frozen=True)
class AppDataRootState:
    """Bounded metadata about one candidate Bridge data root."""

    root: Path
    state_categories: tuple[str, ...] = ()
    persistent_categories: tuple[str, ...] = ()
    exists: bool = False
    inactive: bool = False

    @property
    def has_state(self) -> bool:
        return bool(self.state_categories)

    @property
    def has_persistent_state(self) -> bool:
        return bool(self.persistent_categories)

    def as_dict(self) -> dict[str, object]:
        return {
            "root": str(self.root),
            "exists": self.exists,
            "inactive": self.inactive,
            "state_categories": list(self.state_categories),
            "persistent_categories": list(self.persistent_categories),
        }


@dataclass(frozen=True)
class AppDataRootReport:
    """Canonical/legacy root relationship used by Doctor and repairs."""

    canonical_root: Path
    canonical_state: AppDataRootState
    legacy_roots: tuple[AppDataRootState, ...] = ()
    alias_roots: tuple[Path, ...] = ()
    resolution_source: str = "unknown"

    @property
    def legacy_root_detected(self) -> bool:
        return bool(self.legacy_roots or self.alias_roots)

    @property
    def active_legacy_roots(self) -> tuple[AppDataRootState, ...]:
        return tuple(root for root in self.legacy_roots if not root.inactive)

    @property
    def split_brain(self) -> bool:
        return self.canonical_state.has_persistent_state and any(
            root.has_persistent_state for root in self.active_legacy_roots
        )

    @property
    def migration_available(self) -> bool:
        return (
            not self.canonical_state.has_persistent_state
            and len(self.active_legacy_roots) == 1
            and self.active_legacy_roots[0].has_persistent_state
        )

    @property
    def status(self) -> str:
        if self.split_brain:
            return "SPLIT_BRAIN_DETECTED"
        if self.migration_available:
            return "MIGRATION_AVAILABLE"
        if self.active_legacy_roots:
            return "LEGACY_ROOT_DETECTED"
        return "CLEAN"

    def as_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "resolution_source": self.resolution_source,
            "canonical_root": str(self.canonical_root),
            "canonical_state": self.canonical_state.as_dict(),
            "legacy_root_detected": self.legacy_root_detected,
            "legacy_roots": [root.as_dict() for root in self.legacy_roots],
            "active_legacy_roots": [root.as_dict() for root in self.active_legacy_roots],
            "alias_roots": [str(root) for root in self.alias_roots],
            "migration_available": self.migration_available,
            "split_brain": self.split_brain,
        }


class AppDataMigrationError(RuntimeError):
    """Raised when legacy state cannot be migrated without ambiguity."""


@dataclass(frozen=True)
class AppDataPaths:
    """Persistent application paths, never rooted in a user project checkout."""

    root: Path
    data: Path
    logs: Path
    runtime: Path
    config: Path
    cache: Path
    components: Path
    legacy_roots: tuple[Path, ...] = ()
    alias_roots: tuple[Path, ...] = ()
    resolution_source: str = "unknown"
    # ``root`` is the stable canonical path. Packaged processes can require a
    # physical package alias for file I/O because Windows redirects the
    # canonical path into the current package's LocalCache view.
    physical_root: Path | None = None

    @classmethod
    def from_environment(
        cls,
        *,
        home: Path | None = None,
        environ: Mapping[str, str] | None = None,
        system: str | None = None,
        known_folder_resolver: Callable[[], Path | None] | None = None,
    ) -> "AppDataPaths":
        env = os.environ if environ is None else environ
        system_name = system or platform.system()
        override = env.get("CODEX_SUPERVISOR_DATA_DIR", "").strip()
        if override:
            root = Path(override).expanduser()
            source = "explicit_override"
            legacy_roots: tuple[Path, ...] = ()
            alias_roots: tuple[Path, ...] = ()
            physical_root = root
        elif system_name == "Windows":
            local_app_data, source = resolve_windows_local_app_data(
                environ=env,
                home=home,
                known_folder_resolver=known_folder_resolver,
            )
            root = local_app_data
            root /= "CodexSupervisorBridge"
            legacy_roots, alias_roots = _discover_windows_legacy_roots(
                canonical_root=root,
                environ=env,
            )
            physical_root, legacy_roots, alias_roots = _converge_packaged_root(
                canonical_root=root,
                legacy_roots=legacy_roots,
                alias_roots=alias_roots,
            )
            if not _same_path(physical_root, root):
                source = f"{source}_packaged_alias"
        elif env.get("XDG_DATA_HOME", "").strip():
            root = Path(env["XDG_DATA_HOME"]) / "codex-supervisor-bridge"
            source = "xdg_data_home"
            legacy_roots = ()
            alias_roots = ()
            physical_root = root
        else:
            root = (home or Path.home()) / ".local" / "share" / "codex-supervisor-bridge"
            source = "portable_home"
            legacy_roots = ()
            alias_roots = ()
            physical_root = root
        return cls(
            root=root,
            data=physical_root / "data",
            logs=physical_root / "logs",
            runtime=physical_root / "runtime",
            config=physical_root / "config",
            cache=physical_root / "cache",
            components=physical_root / "components",
            legacy_roots=legacy_roots,
            alias_roots=alias_roots,
            resolution_source=source,
            physical_root=physical_root,
        )

    @property
    def filesystem_root(self) -> Path:
        """Return the root that should be used for local file I/O."""

        return self.physical_root or self.root

    @property
    def root_report(self) -> AppDataRootReport:
        return inspect_app_data_roots(
            self.root,
            canonical_storage_root=self.filesystem_root,
            legacy_roots=self.legacy_roots,
            alias_roots=self.alias_roots,
            resolution_source=self.resolution_source,
        )

    def canonicalize_path(self, value: str | Path) -> Path:
        """Keep app-data paths lexical and map known packaged aliases back here."""

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return candidate
        try:
            relative_to_canonical = candidate.relative_to(self.root)
        except ValueError:
            relative_to_canonical = None
        if relative_to_canonical is not None:
            return self.filesystem_root / relative_to_canonical
        relative = self.alias_relative_path(candidate)
        if relative is not None:
            return self.filesystem_root / relative
        return candidate

    def alias_relative_path(self, value: str | Path) -> Path | None:
        """Return the suffix of a path under a discovered packaged alias."""

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            return None
        candidate = candidate.absolute()
        for alias in self.alias_roots:
            alias_absolute = alias.expanduser().absolute()
            try:
                return candidate.relative_to(alias_absolute)
            except ValueError:
                continue
        return None

    @property
    def database(self) -> Path:
        return self.data / "supervisor.db"

    @property
    def settings(self) -> Path:
        return self.config / "settings.json"

    @property
    def generated_mcp_config(self) -> Path:
        return self.config / "mcp.json"

    def ensure_directories(self) -> None:
        for path in (
            self.filesystem_root,
            self.data,
            self.logs,
            self.runtime,
            self.config,
            self.cache,
            self.components,
        ):
            path.mkdir(parents=True, exist_ok=True)


def resolve_windows_local_app_data(
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    known_folder_resolver: Callable[[], Path | None] | None = None,
) -> tuple[Path, str]:
    """Resolve stable per-user Local AppData without trusting package virtualization."""

    env = os.environ if environ is None else environ
    resolver = known_folder_resolver or _known_folder_local_app_data
    try:
        known_folder = resolver()
    except Exception:  # pragma: no cover - defensive boundary around Win32 APIs
        known_folder = None
    if known_folder is not None and not _is_packaged_local_app_data(str(known_folder)):
        return Path(known_folder).expanduser(), "known_folder"

    profile = env.get("USERPROFILE", "").strip()
    if home is not None:
        stable = Path(home).expanduser() / "AppData" / "Local"
        return stable, "home_fallback"
    if profile:
        return Path(profile).expanduser() / "AppData" / "Local", "userprofile_fallback"

    fallback = env.get("LOCALAPPDATA", "").strip()
    if fallback and not _is_packaged_local_app_data(fallback):
        return Path(fallback).expanduser(), "localappdata_fallback"
    return Path.home() / "AppData" / "Local", "home_fallback"


def inspect_app_data_roots(
    canonical_root: str | Path,
    *,
    canonical_storage_root: str | Path | None = None,
    legacy_roots: tuple[Path, ...] = (),
    alias_roots: tuple[Path, ...] = (),
    resolution_source: str = "unknown",
) -> AppDataRootReport:
    canonical = Path(canonical_root)
    storage = Path(canonical_storage_root) if canonical_storage_root is not None else canonical
    states = tuple(
        _inspect_root(root)
        for root in legacy_roots
        if not _same_path(canonical, root)
    )
    canonical_state = _inspect_root(storage)
    if not _same_path(canonical, storage):
        canonical_state = replace(canonical_state, root=canonical)
    return AppDataRootReport(
        canonical_root=canonical,
        canonical_state=canonical_state,
        legacy_roots=states,
        alias_roots=tuple(alias_roots),
        resolution_source=resolution_source,
    )


def migrate_legacy_app_data(paths: AppDataPaths) -> AppDataRootReport:
    """Copy one unambiguous legacy state into the canonical root atomically."""

    report = paths.root_report
    if report.split_brain:
        raise AppDataMigrationError("SPLIT_BRAIN_DETECTED: persistent state exists in both roots")
    if not report.migration_available:
        raise AppDataMigrationError(
            f"legacy migration is unavailable for root status {report.status}"
        )
    legacy = report.active_legacy_roots[0]
    if _legacy_runtime_is_live(legacy.root):
        raise AppDataMigrationError("legacy runtime contains live process state")

    backup = paths.filesystem_root / ".migration-backups" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    )
    backup.mkdir(parents=True, exist_ok=False)
    copied: list[Path] = []
    try:
        for name in PERSISTENT_STATE_NAMES:
            relative = STATE_PATHS[name]
            source = legacy.root / relative
            if not _has_content(source):
                continue
            _copy_tree_or_file(source, backup / relative)
            target = paths.filesystem_root / relative
            if _has_content(target):
                raise AppDataMigrationError(
                    f"canonical state appeared during migration: {relative}"
                )
            _copy_tree_or_file_atomic(source, target)
            copied.append(target)
        migrated = inspect_app_data_roots(
            paths.root,
            canonical_storage_root=paths.filesystem_root,
            legacy_roots=paths.legacy_roots,
            alias_roots=paths.alias_roots,
            resolution_source=paths.resolution_source,
        )
        if not migrated.canonical_state.has_persistent_state:
            raise AppDataMigrationError("canonical state validation failed after migration")
        _validate_migrated_state(paths.filesystem_root, migrated.canonical_state.persistent_categories)
        _write_legacy_inactive_marker(legacy.root, paths.root, backup)
    except Exception:
        for target in reversed(copied):
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target, ignore_errors=True)
            else:
                target.unlink(missing_ok=True)
        raise
    return inspect_app_data_roots(
        paths.root,
        canonical_storage_root=paths.filesystem_root,
        legacy_roots=paths.legacy_roots,
        alias_roots=paths.alias_roots,
        resolution_source=paths.resolution_source,
    )


def _inspect_root(root: Path) -> AppDataRootState:
    exists = root.exists()
    inactive = (root / LEGACY_INACTIVE_MARKER).is_file()
    categories = tuple(name for name, relative in STATE_PATHS.items() if _has_content(root / relative))
    persistent = tuple(name for name in PERSISTENT_STATE_NAMES if name in categories)
    if inactive:
        persistent = ()
    return AppDataRootState(
        root=root,
        state_categories=categories,
        persistent_categories=persistent,
        exists=exists,
        inactive=inactive,
    )


def _has_content(path: Path) -> bool:
    if not path.exists():
        return False
    if path.is_file():
        return True
    try:
        return any(path.iterdir())
    except OSError:
        return True


def _is_packaged_local_app_data(value: str) -> bool:
    normalized = value.replace("/", "\\").rstrip("\\")
    parts = [part.casefold() for part in normalized.split("\\") if part]
    for index in range(len(parts) - 5):
        if parts[index : index + 3] == ["appdata", "local", "packages"] and parts[index + 4 : index + 6] == ["localcache", "local"]:
            return True
    return False


def _discover_windows_legacy_roots(
    *,
    canonical_root: Path,
    environ: Mapping[str, str],
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    candidates: list[Path] = []
    reported = environ.get("LOCALAPPDATA", "").strip()
    if reported and _is_packaged_local_app_data(reported):
        candidates.append(Path(reported) / "CodexSupervisorBridge")

    stable_local = canonical_root.parent
    packages = stable_local / "Packages"
    if packages.is_dir():
        try:
            for package in packages.iterdir():
                candidates.append(package / "LocalCache" / "Local" / "CodexSupervisorBridge")
        except OSError:
            pass

    unique: list[Path] = []
    for candidate in candidates:
        if _same_path(canonical_root, candidate):
            continue
        if not candidate.exists():
            continue
        if any(_path_key(candidate) == _path_key(existing) for existing in unique):
            continue
        unique.append(candidate)
    aliases: list[Path] = []
    legacy: list[Path] = []
    for candidate in unique:
        if _same_physical_path(canonical_root, candidate):
            aliases.append(candidate)
        elif _inspect_root(candidate).has_state:
            legacy.append(candidate)
    return tuple(legacy), tuple(aliases)


def _converge_packaged_root(
    *,
    canonical_root: Path,
    legacy_roots: tuple[Path, ...],
    alias_roots: tuple[Path, ...],
) -> tuple[Path, tuple[Path, ...], tuple[Path, ...]]:
    """Recover the physical canonical alias after package virtualization.

    A packaged Python process can see the stable canonical path as its own
    package-local directory.  We only redirect file I/O when that view is
    already marked inactive by an explicit canonical reconciliation, the
    marker's backup evidence is present in exactly one other package root, and
    no ambiguous second active root exists.  This is path-view convergence,
    not authority selection for a live split brain.
    """

    current_aliases = [
        alias
        for alias in alias_roots
        if _same_physical_path(canonical_root, alias)
    ]
    if len(current_aliases) != 1:
        return canonical_root, legacy_roots, alias_roots
    current_alias = current_aliases[0]
    marker = _read_canonical_inactive_marker(current_alias, canonical_root)
    if marker is None:
        return canonical_root, legacy_roots, alias_roots

    active = [
        root
        for root in legacy_roots
        if not _inspect_root(root).inactive
        and _inspect_root(root).has_persistent_state
        and _is_packaged_bridge_root(root)
    ]
    if len(active) != 1 or not _backup_evidence_matches(active[0], marker):
        return canonical_root, legacy_roots, alias_roots

    selected = active[0]
    remaining_legacy = tuple(
        root for root in legacy_roots if not _same_path(root, selected)
    )
    if not any(_same_path(root, current_alias) for root in remaining_legacy):
        remaining_legacy = (*remaining_legacy, current_alias)
    aliases = list(alias_roots)
    if not any(_same_path(root, selected) for root in aliases):
        aliases.append(selected)
    return selected, remaining_legacy, tuple(aliases)


def _is_packaged_bridge_root(root: Path) -> bool:
    return _is_packaged_local_app_data(str(root))


def _read_canonical_inactive_marker(
    root: Path,
    canonical_root: Path,
) -> dict[str, object] | None:
    marker_path = root / LEGACY_INACTIVE_MARKER
    try:
        payload = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("authority") != "canonical":
        return None
    if payload.get("reason") != "explicit_split_brain_reconciliation":
        return None
    marker_canonical = payload.get("canonical_root")
    if not isinstance(marker_canonical, str):
        return None
    if _path_key(Path(marker_canonical)) != _path_key(canonical_root):
        return None
    if not isinstance(payload.get("backup_location"), str):
        return None
    return payload


def _backup_evidence_matches(
    candidate: Path,
    marker: Mapping[str, object],
) -> bool:
    backup_location = marker.get("backup_location")
    if not isinstance(backup_location, str):
        return False
    backup_name = Path(backup_location).parent.name
    if not backup_name:
        return False
    evidence = candidate / ".reconciliation-backups" / backup_name / "legacy"
    return evidence.is_dir()


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _same_physical_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except (OSError, ValueError):
        return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path))).casefold()


def _known_folder_local_app_data() -> Path | None:
    if os.name != "nt":
        return None
    import ctypes
    import uuid as uuid_module

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    guid = GUID.from_buffer_copy(
        uuid_module.UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091").bytes_le
    )
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    result = ctypes.c_wchar_p()
    status = shell32.SHGetKnownFolderPath(
        ctypes.byref(guid),
        0,
        None,
        ctypes.byref(result),
    )
    if status != 0 or not result.value:
        return None
    try:
        return Path(result.value)
    finally:
        ole32.CoTaskMemFree(result)


def _copy_tree_or_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.is_dir():
        shutil.copytree(source, target)
        _fsync_tree(target)
    else:
        shutil.copy2(source, target)
        _fsync_file(target)


def _copy_tree_or_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        _copy_tree_or_file(source, temporary)
        if target.is_dir() and not target.is_symlink():
            # Canonical bootstrap may have created an empty directory already;
            # replacing only that harmless placeholder keeps promotion atomic.
            if any(target.iterdir()):
                raise AppDataMigrationError(
                    f"canonical state appeared during migration: {target.name}"
                )
            target.rmdir()
        os.replace(temporary, target)
        _fsync_directory(target.parent)
    finally:
        if temporary.is_dir() and not temporary.is_symlink():
            shutil.rmtree(temporary, ignore_errors=True)
        else:
            temporary.unlink(missing_ok=True)


def _fsync_file(path: Path) -> None:
    try:
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
    except OSError:
        pass


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError:
        pass


def _fsync_tree(path: Path) -> None:
    if path.is_dir():
        for child in path.iterdir():
            _fsync_tree(child)
        _fsync_directory(path)
    else:
        _fsync_file(path)


def _legacy_runtime_is_live(root: Path) -> bool:
    state_path = root / "runtime" / "processes.json"
    try:
        import json

        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    return any(
        isinstance(item, dict)
        and item.get("status") in {"RUNNING", "UNKNOWN"}
        and isinstance(item.get("pid"), int)
        for item in payload.values()
    )


def _validate_migrated_state(root: Path, categories: tuple[str, ...]) -> None:
    import json
    import sqlite3

    if "settings" in categories:
        json.loads((root / STATE_PATHS["settings"]).read_text(encoding="utf-8"))
    if "database" in categories:
        database = root / STATE_PATHS["database"]
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA schema_version").fetchone()
        finally:
            connection.close()
    if "secrets" in categories and not (root / STATE_PATHS["secrets"]).is_dir():
        raise AppDataMigrationError("SecretStore validation failed after migration")


def _write_legacy_inactive_marker(root: Path, canonical: Path, backup: Path) -> None:
    payload = {
        "canonical_root": str(canonical),
        "backup": str(backup),
        "marked_at": datetime.now(timezone.utc).isoformat(),
        "reason": "migrated_to_canonical_root",
    }
    temporary = root / f".{LEGACY_INACTIVE_MARKER}.{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        _fsync_file(temporary)
        os.replace(temporary, root / LEGACY_INACTIVE_MARKER)
        _fsync_directory(root)
    finally:
        temporary.unlink(missing_ok=True)
