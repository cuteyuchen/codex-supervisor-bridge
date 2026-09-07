from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, Field

from codex_supervisor_bridge.memory.codex_runtime import ACTIVE_RUNTIME_STATUSES

from .configuration import AppConfig, ConfigStore
from .paths import (
    LEGACY_INACTIVE_MARKER,
    PERSISTENT_STATE_NAMES,
    STATE_PATHS,
    AppDataPaths,
    AppDataRootReport,
    AppDataRootState,
    inspect_app_data_roots,
)
from .physical import PhysicalPathGuard, PhysicalPathVerificationError
from .process import (
    _pid_exists,
    _process_identity,
    classify_persisted_process,
)

_PLAN_PREFIX = "rpl1_"
_PLAN_VERSION = 1
_HASH_LIMIT = 64 * 1024 * 1024
_IGNORED_FINGERPRINT_DIRS = {".reconciliation-backups", ".migration-backups"}
_ACTIVE_TASK_STATUSES = {"active", "in_progress", "running", "started", "paused"}


class ReconciliationError(RuntimeError):
    """Raised for invalid reconciliation input before any filesystem mutation."""


class ReconciliationPlan(BaseModel):
    """A two-phase, portable description of one explicit root reconciliation."""

    plan_id: str
    created_at: datetime
    canonical_root: str
    selected_authority: str
    other_root: str
    canonical_state_categories: list[str] = Field(default_factory=list)
    other_state_categories: list[str] = Field(default_factory=list)
    canonical_persistent_categories: list[str] = Field(default_factory=list)
    other_persistent_categories: list[str] = Field(default_factory=list)
    settings_relation: str
    secret_name_relation: str
    database_relation: str
    runtime_relation: str
    actions: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    safe_to_apply: bool = False
    canonical_root_fingerprint: str
    other_root_fingerprint: str
    canonical_secret_summary: dict[str, Any] = Field(default_factory=dict)
    other_secret_summary: dict[str, Any] = Field(default_factory=dict)
    secret_name_comparison: dict[str, Any] = Field(default_factory=dict)
    secret_values_compared: bool = False
    settings_differences: dict[str, Any] = Field(default_factory=dict)
    database_metadata: dict[str, Any] = Field(default_factory=dict)
    other_database_metadata: dict[str, Any] = Field(default_factory=dict)
    runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    other_runtime_metadata: dict[str, Any] = Field(default_factory=dict)
    already_inactive: bool = False

    def user_view(self) -> dict[str, Any]:
        """Compact output suitable for the normal CLI experience."""

        return {
            "status": "PLAN_READY" if self.safe_to_apply else "PLAN_BLOCKED",
            "plan_id": self.plan_id,
            "selected_authority": self.selected_authority,
            "safe_to_apply": self.safe_to_apply,
            "settings_relation": self.settings_relation,
            "secret_name_relation": self.secret_name_relation,
            "database_relation": self.database_relation,
            "runtime_relation": self.runtime_relation,
            "actions": list(self.actions),
            "blocking_reasons": list(self.blocking_reasons),
        }

    def advanced_view(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ReconciliationResult(BaseModel):
    """Result of a dry-run plan or a confirmed apply."""

    status: str
    message: str
    plan: ReconciliationPlan | None = None
    applied: bool = False
    idempotent: bool = False
    backup_location: str | None = None
    inactive_marker: str | None = None
    root_report: dict[str, Any] | None = None
    blocking_reasons: list[str] = Field(default_factory=list)
    error: str | None = None

    def user_view(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "message": self.message,
            "applied": self.applied,
            "idempotent": self.idempotent,
            "blocking_reasons": list(self.blocking_reasons),
        }
        if self.plan is not None:
            payload.update(self.plan.user_view())
        return payload

    def advanced_view(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        if self.plan is not None:
            payload["plan"] = self.plan.advanced_view()
        return payload


class ReconciliationService:
    """Explicit, fail-closed reconciliation of two application-data roots."""

    def __init__(
        self,
        *,
        paths: AppDataPaths | None = None,
        clock: Callable[[], datetime] | None = None,
        nonce_factory: Callable[[], str] | None = None,
        pid_exists: Callable[[int], bool] | None = None,
        process_identity: Callable[[int], dict[str, Any] | None] | None = None,
        path_guard: PhysicalPathGuard | None = None,
    ) -> None:
        self.paths = paths or AppDataPaths.from_environment()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._nonce = nonce_factory or (lambda: uuid.uuid4().hex)
        self._pid_exists = pid_exists or _pid_exists
        self._process_identity = process_identity or _process_identity
        self.path_guard = path_guard or PhysicalPathGuard()

    def plan(
        self,
        *,
        selected_authority: str = "canonical",
        legacy_root: str | Path | None = None,
    ) -> ReconciliationPlan:
        authority = selected_authority.strip().lower()
        if authority not in {"canonical", "legacy"}:
            raise ReconciliationError(
                "selected authority must be 'canonical' or 'legacy'"
            )
        canonical = self.paths.filesystem_root.absolute()
        canonical_display = self.paths.root.absolute()
        self._verify_root_for_read(canonical, self.path_guard, role="app_data")
        if legacy_root is None:
            for discovered in self.paths.legacy_roots:
                self._verify_root_for_read(
                    discovered.absolute(),
                    self.path_guard.for_legacy_migration(),
                    role="legacy",
                )
        other = self._select_other_root(legacy_root)
        other = other.absolute()
        self._verify_root_for_read(
            other,
            self.path_guard.for_legacy_migration(),
            role="legacy",
        )
        report = self._report(other)
        canonical_state = report.canonical_state
        other_state = self._state_for(report, other)
        canonical_fingerprint = _root_fingerprint(canonical)
        other_fingerprint = _root_fingerprint(other)
        canonical_settings = _settings_snapshot(canonical, self.paths)
        other_settings = _settings_snapshot(other, self.paths)
        canonical_secrets = _secret_summary(canonical)
        other_secrets = _secret_summary(other)
        secret_name_comparison = _secret_name_comparison(
            canonical_secrets["names"], other_secrets["names"]
        )
        canonical_database = _database_snapshot(canonical)
        other_database = _database_snapshot(other)
        canonical_runtime = _runtime_snapshot(
            canonical,
            self._pid_exists,
            self._process_identity,
            path_guard=self.path_guard,
            role="runtime",
        )
        other_runtime = _runtime_snapshot(
            other,
            self._pid_exists,
            self._process_identity,
            path_guard=self.path_guard.for_legacy_migration(),
            role="runtime",
            allow_packaged_legacy=True,
        )

        blocking: list[str] = []
        actions: list[str] = []
        already_inactive = other_state.inactive

        if _same_physical_path(canonical, other):
            blocking.append("ROOTS_ARE_PHYSICAL_ALIAS")
        if not other.exists:
            blocking.append("LEGACY_ROOT_NOT_FOUND")
        if authority == "legacy":
            # The model deliberately accepts the future authority, while this
            # round keeps promotion conservative and canonical-only.
            blocking.append("LEGACY_AUTHORITY_PROMOTION_NOT_ENABLED")
        if not canonical_state.has_persistent_state and not already_inactive:
            blocking.append("CANONICAL_ROOT_HAS_NO_PERSISTENT_STATE")
        if not other_state.has_persistent_state and not already_inactive:
            blocking.append("LEGACY_ROOT_HAS_NO_PERSISTENT_STATE")

        if not already_inactive:
            if other_database["exists"] and _database_has_active_state(other_database):
                blocking.append("RECONCILIATION_BLOCKED_ACTIVE_STATE")
            if (
                other_runtime["live_pids"]
                or other_runtime["unknown_live_pids"]
                or other_runtime["live_locks"]
                or other_runtime["unknown_locks"]
            ):
                blocking.append("BLOCKED_LIVE_LEGACY_RUNTIME")
            if (
                canonical_runtime["live_pids"]
                or canonical_runtime["unknown_live_pids"]
                or canonical_runtime["live_locks"]
                or canonical_runtime["unknown_locks"]
            ):
                blocking.append("BLOCKED_LIVE_CANONICAL_RUNTIME")
            if not canonical_runtime["valid"] or not other_runtime["valid"]:
                blocking.append("INVALID_RUNTIME_METADATA")

        if already_inactive:
            actions.append("already_inactive")
        elif not blocking:
            actions.extend(
                [
                    "backup_non_authoritative_root",
                    "validate_backup",
                    "write_inactive_marker",
                    "re_run_doctor",
                ]
            )

        created_at = self._clock()
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        plan_id = _make_plan_id(
            selected_authority=authority,
            canonical_fingerprint=canonical_fingerprint,
            other_fingerprint=other_fingerprint,
            created_at=created_at,
            nonce=self._nonce(),
        )
        return ReconciliationPlan(
            plan_id=plan_id,
            created_at=created_at,
            canonical_root=str(canonical_display),
            selected_authority=authority,
            other_root=str(other),
            canonical_state_categories=list(canonical_state.state_categories),
            other_state_categories=list(other_state.state_categories),
            canonical_persistent_categories=list(canonical_state.persistent_categories),
            other_persistent_categories=list(other_state.persistent_categories),
            settings_relation=_relation_for_settings(canonical_settings, other_settings),
            secret_name_relation=_relation_for_names(
                canonical_secrets["names"], other_secrets["names"]
            ),
            database_relation=_relation_for_database(
                canonical_database, other_database
            ),
            runtime_relation=_relation_for_runtime(
                canonical_runtime, other_runtime
            ),
            actions=actions,
            blocking_reasons=blocking,
            safe_to_apply=not blocking,
            canonical_root_fingerprint=canonical_fingerprint,
            other_root_fingerprint=other_fingerprint,
            canonical_secret_summary=_public_secret_summary(canonical_secrets),
            other_secret_summary=_public_secret_summary(other_secrets),
            secret_name_comparison=secret_name_comparison,
            secret_values_compared=False,
            settings_differences=_settings_differences(
                canonical_settings, other_settings
            ),
            database_metadata=_public_database_metadata(canonical_database),
            other_database_metadata=_public_database_metadata(other_database),
            runtime_metadata=_public_runtime_metadata(canonical_runtime),
            other_runtime_metadata=_public_runtime_metadata(other_runtime),
            already_inactive=already_inactive,
        )

    def apply(
        self,
        *,
        plan_id: str,
        selected_authority: str | None = None,
        legacy_root: str | Path | None = None,
    ) -> ReconciliationResult:
        try:
            encoded = _decode_plan_id(plan_id)
        except ReconciliationError as exc:
            return ReconciliationResult(
                status="INVALID_RECONCILIATION_PLAN",
                message="The reconciliation plan is invalid.",
                blocking_reasons=["INVALID_RECONCILIATION_PLAN"],
                error=str(exc),
            )

        authority = (selected_authority or str(encoded["authority"])).strip().lower()
        if authority != encoded["authority"]:
            return ReconciliationResult(
                status="INVALID_RECONCILIATION_PLAN",
                message="The selected authority does not match the plan.",
                blocking_reasons=["AUTHORITY_DOES_NOT_MATCH_PLAN"],
            )
        if legacy_root is None:
            return ReconciliationResult(
                status="INVALID_RECONCILIATION_PLAN",
                message="The legacy root must be supplied when applying a plan.",
                blocking_reasons=["LEGACY_ROOT_REQUIRED"],
            )

        try:
            current = self.plan(
                selected_authority=authority,
                legacy_root=legacy_root,
            )
        except ReconciliationError as exc:
            return ReconciliationResult(
                status="INVALID_RECONCILIATION_PLAN",
                message="The reconciliation roots are no longer available.",
                blocking_reasons=["RECONCILIATION_PLAN_UNAVAILABLE"],
                error=str(exc),
            )
        try:
            created_at = _decode_created_at(encoded)
        except ReconciliationError as exc:
            return ReconciliationResult(
                status="INVALID_RECONCILIATION_PLAN",
                message="The reconciliation plan timestamp is invalid.",
                blocking_reasons=["INVALID_RECONCILIATION_PLAN"],
                error=str(exc),
            )
        current_plan = current.model_copy(
            update={
                "plan_id": plan_id,
                "created_at": created_at,
            }
        )
        if (
            current.canonical_root_fingerprint != encoded["canonical_fingerprint"]
            or current.other_root_fingerprint != encoded["other_fingerprint"]
        ):
            return ReconciliationResult(
                status="STALE_RECONCILIATION_PLAN",
                message="The roots changed after this plan was created; create a new plan.",
                plan=current_plan,
                blocking_reasons=["STALE_RECONCILIATION_PLAN"],
            )
        if not current.safe_to_apply:
            status = current.blocking_reasons[0] if current.blocking_reasons else "RECONCILIATION_BLOCKED"
            return ReconciliationResult(
                status=status,
                message="The reconciliation plan is not safe to apply.",
                plan=current_plan,
                blocking_reasons=list(current.blocking_reasons),
            )
        if current.already_inactive:
            report = self._report(Path(current.other_root))
            return ReconciliationResult(
                status="ALREADY_RECONCILED",
                message="The non-authoritative root is already inactive; no destructive action was taken.",
                plan=current_plan,
                idempotent=True,
                root_report=report.as_dict(),
            )

        backup_location: Path | None = None
        try:
            legacy_guard = self.path_guard.for_legacy_migration()
            self.path_guard.verify_root(self.paths.filesystem_root, role="app_data")
            legacy_guard.verify_root(
                Path(current.other_root),
                role="legacy",
                require_directory=True,
                allow_packaged_legacy=True,
            )
            backup_location = self._create_backup(
                source=Path(current.other_root),
                plan=current_plan,
                canonical_root=self.paths.filesystem_root,
                path_guard=self.path_guard,
            )
            # The source must still be exactly the one approved before the
            # backup is promoted to evidence. A changed source stays active.
            if (
                _root_fingerprint(self.paths.filesystem_root.absolute())
                != current.canonical_root_fingerprint
                or _root_fingerprint(Path(current.other_root))
                != current.other_root_fingerprint
            ):
                return ReconciliationResult(
                    status="STALE_RECONCILIATION_PLAN",
                    message="The legacy root changed while it was being backed up; create a new plan.",
                    plan=current_plan,
                    backup_location=str(backup_location),
                    blocking_reasons=["STALE_RECONCILIATION_PLAN"],
                )
            marker = self._write_inactive_marker(
                root=Path(current.other_root),
                canonical=Path(current.canonical_root),
                plan=current_plan,
                backup=backup_location,
                path_guard=self.path_guard,
            )
            report = self._report(Path(current.other_root))
            if report.split_brain:
                raise ReconciliationError(
                    "post-reconciliation root report still reports split brain"
                )
            return ReconciliationResult(
                status="RECONCILED",
                message="The canonical root was kept; the other root was backed up and marked inactive.",
                plan=current_plan,
                applied=True,
                backup_location=str(backup_location),
                inactive_marker=str(marker),
                root_report=report.as_dict(),
            )
        except Exception as exc:
            # A backup may be useful evidence after a failed copy or
            # validation. Never turn the legacy root inactive on this path.
            return ReconciliationResult(
                status="RECONCILIATION_FAILED",
                message="Reconciliation did not complete; both roots remain active.",
                plan=current_plan,
                backup_location=str(backup_location) if backup_location else None,
                blocking_reasons=["RECONCILIATION_FAILED"],
                error=str(exc),
            )

    def _select_other_root(self, legacy_root: str | Path | None) -> Path:
        if legacy_root is not None:
            return Path(legacy_root).expanduser()
        report = self.paths.root_report
        candidates = report.active_legacy_roots
        if not candidates:
            candidates = report.legacy_roots
        if len(candidates) != 1:
            raise ReconciliationError(
                "an explicit legacy root is required when zero or multiple roots are present"
            )
        return candidates[0].root

    def _report(self, other: Path) -> AppDataRootReport:
        roots = [self.paths.root]
        if not _same_path(self.paths.root, other):
            roots.append(other)
        return inspect_app_data_roots(
            self.paths.root,
            canonical_storage_root=self.paths.filesystem_root,
            legacy_roots=tuple(roots[1:]),
            alias_roots=self.paths.alias_roots,
            resolution_source=self.paths.resolution_source,
        )

    @staticmethod
    def _verify_root_for_read(
        root: Path,
        guard: PhysicalPathGuard,
        *,
        role: str,
    ) -> None:
        """Prove a reconciliation root before any metadata or content read."""

        try:
            evidence = guard.verify_root(
                root,
                role=role,
                require_directory=True,
                allow_packaged_legacy=role == "legacy",
            )
            if evidence.exists:
                guard.verify_tree(
                    root,
                    role=role,
                    allow_packaged_legacy=role == "legacy",
                )
        except PhysicalPathVerificationError as exc:
            raise ReconciliationError(
                f"{role} root physical path verification failed: {exc}"
            ) from exc

    @staticmethod
    def _state_for(report: AppDataRootReport, root: Path) -> AppDataRootState:
        if _same_path(report.canonical_root, root):
            return report.canonical_state
        for item in report.legacy_roots:
            if _same_path(item.root, root):
                return item
        return AppDataRootState(root=root)

    def _create_backup(
        self,
        *,
        source: Path,
        plan: ReconciliationPlan,
        canonical_root: Path | None = None,
        path_guard: PhysicalPathGuard | None = None,
    ) -> Path:
        guard = path_guard or self.path_guard
        source_guard = guard.for_legacy_migration()
        parent = (canonical_root or Path(plan.canonical_root)) / ".reconciliation-backups"
        guard.ensure_directory(parent, role="app_data")
        stamp = plan.created_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        suffix = hashlib.sha256(plan.plan_id.encode("utf-8")).hexdigest()[:12]
        final_dir = parent / f"{stamp}-{suffix}"
        staging = parent / f".pending-{uuid.uuid4().hex}"
        guard.ensure_directory(staging, role="app_data")
        try:
            target = staging / "legacy"
            source_guard.verify_tree(
                source,
                role="legacy",
                allow_packaged_legacy=True,
            )
            guard.copy_tree(
                source,
                target,
                source_guard=source_guard,
                source_role="legacy",
                role="app_data",
                allow_packaged_legacy=True,
            )
            guard.verify_tree(target, role="app_data")
            _validate_backup(source, target)
            metadata = {
                "version": 1,
                "created_at": plan.created_at.isoformat(),
                "reconciliation_plan_id": plan.plan_id,
                "source_root": str(source),
                "canonical_root": plan.canonical_root,
                "authority": plan.selected_authority,
                "source_fingerprint": plan.other_root_fingerprint,
                "state_categories": list(plan.other_state_categories),
                "persistent_categories": list(plan.other_persistent_categories),
                "validation": "passed",
            }
            metadata_path = staging / "reconciliation-metadata.json"
            guard.write_text(
                metadata_path,
                json.dumps(metadata, indent=2, sort_keys=True) + "\n",
                role="app_data",
            )
            _fsync_file(metadata_path)
            _fsync_tree(staging)
            guard.before_write(final_dir, role="app_data")
            guard.replace(staging, final_dir, role="app_data")
            _fsync_directory(parent)
            return final_dir / "legacy"
        except Exception:
            # Keep the staging directory as backup evidence for diagnostics.
            raise

    @staticmethod
    def _write_inactive_marker(
        *,
        root: Path,
        canonical: Path,
        plan: ReconciliationPlan,
        backup: Path,
        path_guard: PhysicalPathGuard | None = None,
    ) -> Path:
        guard = path_guard or PhysicalPathGuard()
        marker_guard = guard.for_legacy_migration()
        marker = root / LEGACY_INACTIVE_MARKER
        temporary = root / f".{LEGACY_INACTIVE_MARKER}.{uuid.uuid4().hex}.tmp"
        payload = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "canonical_root": str(canonical),
            "reconciliation_plan_id": plan.plan_id,
            "authority": plan.selected_authority,
            "state_categories": list(plan.other_state_categories),
            "backup_location": str(backup),
            "reason": "explicit_split_brain_reconciliation",
        }
        marker_guard.ensure_directory(
            root,
            role="legacy",
            allow_packaged_legacy=True,
        )
        descriptor, temporary = marker_guard.create_temp_file(
            root,
            prefix=f".{LEGACY_INACTIVE_MARKER}.",
            suffix=".tmp",
            role="legacy",
            allow_packaged_legacy=True,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            marker_guard.replace(
                temporary,
                marker,
                role="legacy",
                allow_packaged_legacy=True,
            )
            _fsync_directory(root)
            return marker
        finally:
            marker_guard.remove(
                temporary,
                role="legacy",
                allow_packaged_legacy=True,
            )


def _make_plan_id(
    *,
    selected_authority: str,
    canonical_fingerprint: str,
    other_fingerprint: str,
    created_at: datetime,
    nonce: str,
) -> str:
    payload = {
        "v": _PLAN_VERSION,
        "authority": selected_authority,
        "canonical_fingerprint": canonical_fingerprint,
        "other_fingerprint": other_fingerprint,
        "created_at": created_at.isoformat(),
        "nonce": nonce,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _PLAN_PREFIX + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_plan_id(plan_id: str) -> dict[str, Any]:
    if not isinstance(plan_id, str) or not plan_id.startswith(_PLAN_PREFIX):
        raise ReconciliationError("unsupported reconciliation plan id")
    encoded = plan_id[len(_PLAN_PREFIX) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
    except (ValueError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReconciliationError("malformed reconciliation plan id") from exc
    required = {"v", "authority", "canonical_fingerprint", "other_fingerprint", "created_at", "nonce"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ReconciliationError("incomplete reconciliation plan id")
    if payload["v"] != _PLAN_VERSION or payload["authority"] not in {"canonical", "legacy"}:
        raise ReconciliationError("unsupported reconciliation plan version")
    for key in ("canonical_fingerprint", "other_fingerprint", "created_at", "nonce"):
        if not isinstance(payload[key], str) or not payload[key]:
            raise ReconciliationError("invalid reconciliation plan binding")
    return payload


def _decode_created_at(payload: dict[str, Any]) -> datetime:
    try:
        value = datetime.fromisoformat(payload["created_at"])
    except (TypeError, ValueError) as exc:
        raise ReconciliationError("invalid plan timestamp") from exc
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _settings_snapshot(root: Path, paths: AppDataPaths) -> dict[str, Any]:
    path = root / STATE_PATHS["settings"]
    if not path.is_file():
        return {"present": False, "valid": True, "semantic": None, "safe": None}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        migrated, _ = ConfigStore._migrate(raw)
        config = AppConfig.model_validate(migrated)
        local_paths = _paths_for_root(root)
        ConfigStore(path=path, paths=local_paths)._normalize_app_data_paths(config)
        semantic = config.model_dump(mode="json")
        return {
            "present": True,
            "valid": True,
            "semantic": semantic,
            "safe": _safe_settings_projection(config),
        }
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
        return {
            "present": True,
            "valid": False,
            "semantic": None,
            "safe": None,
            "error": type(exc).__name__,
        }


def _safe_settings_projection(config: AppConfig) -> dict[str, Any]:
    basic = config.basic.model_dump(mode="json")
    advanced = config.advanced
    # These are the schema fields that are useful in advanced diagnostics and
    # do not contain SecretStore values. OAuth/tunnel detail is intentionally
    # excluded from the rendered diff even when it affects relation status.
    safe_advanced = {
        "executable_paths": dict(advanced.executable_paths),
        "process_commands": dict(advanced.process_commands),
        "local_codex_repository": (
            str(advanced.local_codex_repository)
            if advanced.local_codex_repository is not None
            else None
        ),
        "backend_detail": dict(advanced.backend_detail),
        "ports": dict(advanced.ports),
        "sqlite_path": str(advanced.sqlite_path) if advanced.sqlite_path else None,
        "heartbeat_seconds": advanced.heartbeat_seconds,
        "startup_timeout_seconds": advanced.startup_timeout_seconds,
        "shutdown_timeout_seconds": advanced.shutdown_timeout_seconds,
        "log_level": advanced.log_level,
    }
    return {"basic": basic, "advanced": safe_advanced}


def _relation_for_settings(canonical: dict[str, Any], other: dict[str, Any]) -> str:
    if not canonical["present"] and not other["present"]:
        return "IDENTICAL"
    if canonical["present"] and not other["present"]:
        return "CANONICAL_ONLY" if canonical["valid"] else "INVALID"
    if other["present"] and not canonical["present"]:
        return "LEGACY_ONLY" if other["valid"] else "INVALID"
    if not canonical["valid"] or not other["valid"]:
        return "INVALID"
    return "IDENTICAL" if canonical["semantic"] == other["semantic"] else "DIFFERENT"


def _settings_differences(canonical: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    if not canonical.get("valid") or not other.get("valid"):
        return {}
    left = canonical.get("safe") or {}
    right = other.get("safe") or {}
    differences: dict[str, Any] = {}
    for section in sorted(set(left) | set(right)):
        left_section = left.get(section, {})
        right_section = right.get(section, {})
        if left_section == right_section:
            continue
        differences[section] = {
            "canonical": left_section,
            "legacy": right_section,
        }
    return differences


def _secret_names(root: Path) -> tuple[str, ...]:
    directory = root / STATE_PATHS["secrets"]
    if not directory.is_dir():
        return ()
    names: list[str] = []
    try:
        for item in directory.iterdir():
            if not item.is_file():
                continue
            names.append(item.stem if item.suffix.casefold() == ".dpapi" else item.name)
    except OSError:
        return ()
    return tuple(sorted(set(names), key=str.casefold))


def _secret_summary(root: Path) -> dict[str, Any]:
    names = _secret_names(root)
    return {"names": names, "count": len(names)}


def _public_secret_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "count": int(summary.get("count", 0)),
        "names": list(summary.get("names", ())),
    }


def _secret_name_comparison(
    canonical: tuple[str, ...],
    other: tuple[str, ...],
) -> dict[str, Any]:
    canonical_set = set(canonical)
    other_set = set(other)
    return {
        "canonical_count": len(canonical_set),
        "legacy_count": len(other_set),
        "same_names": sorted(canonical_set & other_set, key=str.casefold),
        "canonical_only_names": sorted(canonical_set - other_set, key=str.casefold),
        "legacy_only_names": sorted(other_set - canonical_set, key=str.casefold),
        "values_compared": False,
    }


def _relation_for_names(canonical: tuple[str, ...], other: tuple[str, ...]) -> str:
    if not canonical and not other:
        return "IDENTICAL"
    if canonical and not other:
        return "CANONICAL_ONLY"
    if other and not canonical:
        return "LEGACY_ONLY"
    return "IDENTICAL" if set(canonical) == set(other) else "DIFFERENT"


def _database_snapshot(root: Path) -> dict[str, Any]:
    path = root / STATE_PATHS["database"]
    base: dict[str, Any] = {
        "exists": path.is_file(),
        "valid": True,
        "schema_version": None,
        "sqlite_user_version": None,
        "task_count": 0,
        "active_task_count": 0,
        "runtime_affinity_count": 0,
        "active_writer_count": 0,
        "active_runtime_count": 0,
        "unresolved_reconciliation_count": 0,
        "prepared_mutation_count": 0,
        "running_command_count": 0,
    }
    if not base["exists"]:
        return base
    try:
        connection = sqlite3.connect(str(path))
        try:
            connection.row_factory = sqlite3.Row
            base["sqlite_user_version"] = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if _table_exists(connection, "schema_meta"):
                row = connection.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                if row is not None:
                    try:
                        base["schema_version"] = int(row[0])
                    except (TypeError, ValueError):
                        base["valid"] = False
            if _table_exists(connection, "supervised_tasks"):
                base["task_count"] = int(
                    connection.execute("SELECT COUNT(*) FROM supervised_tasks").fetchone()[0]
                )
                placeholders = ", ".join("?" for _ in _ACTIVE_TASK_STATUSES)
                base["active_task_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM supervised_tasks WHERE LOWER(status) IN ("
                        + placeholders
                        + ")",
                        tuple(sorted(_ACTIVE_TASK_STATUSES)),
                    ).fetchone()[0]
                )
            if _table_exists(connection, "task_backend_binding"):
                base["runtime_affinity_count"] = int(
                    connection.execute("SELECT COUNT(*) FROM task_backend_binding").fetchone()[0]
                )
            elif _table_exists(connection, "codex_runtime_state"):
                statuses = tuple(sorted(status.lower() for status in ACTIVE_RUNTIME_STATUSES))
                placeholders = ", ".join("?" for _ in statuses)
                base["runtime_affinity_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM codex_runtime_state WHERE LOWER(remote_status) IN ("
                        + placeholders
                        + ")",
                        statuses,
                    ).fetchone()[0]
                )
            if _table_exists(connection, "task_execution_state"):
                base["active_writer_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_execution_state WHERE active_writer <> 'NONE'"
                    ).fetchone()[0]
                )
            if _table_exists(connection, "codex_runtime_state"):
                statuses = tuple(sorted(status.lower() for status in ACTIVE_RUNTIME_STATUSES))
                placeholders = ", ".join("?" for _ in statuses)
                base["active_runtime_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM codex_runtime_state WHERE LOWER(remote_status) IN ("
                        + placeholders
                        + ")",
                        statuses,
                    ).fetchone()[0]
                )
            if _table_exists(connection, "task_agent_safety"):
                base["unresolved_reconciliation_count"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_agent_safety WHERE state <> 'NONE'"
                    ).fetchone()[0]
                )
            if _table_exists(connection, "task_workspace_state"):
                base["unresolved_reconciliation_count"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM task_workspace_state WHERE state = 'RECONCILIATION_REQUIRED'"
                    ).fetchone()[0]
                )
            if _table_exists(connection, "direct_workspace_operations"):
                base["prepared_mutation_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM direct_workspace_operations WHERE status = 'PREPARED'"
                    ).fetchone()[0]
                )
                base["unresolved_reconciliation_count"] += int(
                    connection.execute(
                        "SELECT COUNT(*) FROM direct_workspace_operations WHERE status = 'RECONCILIATION_REQUIRED'"
                    ).fetchone()[0]
                )
            if _table_exists(connection, "direct_command_sessions"):
                base["running_command_count"] = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM direct_command_sessions WHERE status = 'RUNNING'"
                    ).fetchone()[0]
                )
        finally:
            connection.close()
    except (OSError, sqlite3.Error, ValueError, TypeError):
        base["valid"] = False
    return base


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def _database_has_active_state(database: dict[str, Any]) -> bool:
    return any(
        int(database.get(key, 0)) > 0
        for key in (
            "active_writer_count",
            "active_runtime_count",
            "unresolved_reconciliation_count",
            "prepared_mutation_count",
            "running_command_count",
        )
    )


def _public_database_metadata(database: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in database.items()
        if key
        in {
            "exists",
            "valid",
            "schema_version",
            "sqlite_user_version",
            "task_count",
            "active_task_count",
            "runtime_affinity_count",
            "active_writer_count",
            "active_runtime_count",
            "unresolved_reconciliation_count",
            "prepared_mutation_count",
            "running_command_count",
        }
    }


def _relation_for_database(canonical: dict[str, Any], other: dict[str, Any]) -> str:
    if not canonical["valid"] or not other["valid"]:
        return "INVALID"
    if canonical["exists"] and not other["exists"]:
        return "CANONICAL_ONLY"
    if other["exists"] and not canonical["exists"]:
        return "LEGACY_ONLY"
    if not canonical["exists"] and not other["exists"]:
        return "IDENTICAL"
    comparable = (
        "schema_version",
        "sqlite_user_version",
        "task_count",
        "active_task_count",
        "runtime_affinity_count",
        "active_writer_count",
        "active_runtime_count",
        "unresolved_reconciliation_count",
        "prepared_mutation_count",
        "running_command_count",
    )
    return "IDENTICAL" if all(canonical[key] == other[key] for key in comparable) else "DIFFERENT"


def _runtime_snapshot(
    root: Path,
    pid_exists: Callable[[int], bool],
    process_identity: Callable[[int], dict[str, Any] | None] | None = None,
    *,
    path_guard: PhysicalPathGuard | None = None,
    role: str = "runtime",
    allow_packaged_legacy: bool = False,
) -> dict[str, Any]:
    """Read runtime state without confusing PID liveness with ownership."""

    path = root / STATE_PATHS["runtime"] / "processes.json"
    runtime_dir = path.parent
    result: dict[str, Any] = {
        "exists": False,
        "valid": True,
        "entry_count": 0,
        "active_names": [],
        "live_pids": [],
        "unknown_live_pids": [],
        "reused_pids": [],
        "stale_pids": [],
        "lock_names": [],
        "live_locks": [],
        "unknown_locks": [],
        "stale_locks": [],
    }
    if path_guard is not None:
        try:
            path_guard.verify_root(
                runtime_dir,
                role=role,
                require_directory=True,
                allow_packaged_legacy=allow_packaged_legacy,
            )
            if path.exists():
                path_guard.verify_root(
                    path,
                    role=role,
                    allow_packaged_legacy=allow_packaged_legacy,
                )
        except PhysicalPathVerificationError:
            result["valid"] = False
            return result

    result["exists"] = path.is_file()
    statuses_by_name: dict[str, str] = {}
    state_by_name: dict[str, str] = {}
    reader = process_identity or _process_identity
    if result["exists"]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("runtime metadata must be an object")
            result["entry_count"] = len(payload)
            for name, item in payload.items():
                name_text = str(name)
                if not isinstance(item, dict):
                    result["valid"] = False
                    state_by_name[name_text] = "unknown"
                    continue
                status = str(item.get("status", "")).upper()
                statuses_by_name[name_text] = status
                if status not in {"RUNNING", "UNKNOWN"}:
                    state_by_name[name_text] = "stale"
                    continue
                result["active_names"].append(name_text)
                pid = item.get("pid")
                if not isinstance(pid, int) or pid <= 0:
                    result["valid"] = False
                    state_by_name[name_text] = "unknown"
                    continue
                classification = classify_persisted_process(
                    status=status,
                    pid=pid,
                    process_identity=item.get("process_identity"),
                    ownership=item.get("ownership"),
                    pid_exists=pid_exists,
                    identity_reader=reader,
                )
                if classification.live:
                    if classification.pid_reused:
                        result["reused_pids"].append(pid)
                        state_by_name[name_text] = "stale"
                    elif classification.identity_verified and classification.ownership_verified:
                        result["live_pids"].append(pid)
                        state_by_name[name_text] = "live"
                    else:
                        result["unknown_live_pids"].append(pid)
                        state_by_name[name_text] = "unknown"
                else:
                    state_by_name[name_text] = "stale"
                    if classification.status == "STALE":
                        result["stale_pids"].append(pid)
                    else:
                        result["valid"] = False
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            result["valid"] = False

    if runtime_dir.is_dir():
        try:
            lock_names = sorted(
                item.name for item in runtime_dir.glob("*.lock") if item.is_file()
            )
            result["lock_names"] = lock_names
            for lock_name in lock_names:
                process_name = Path(lock_name).stem
                state = state_by_name.get(process_name)
                if state == "live":
                    result["live_locks"].append(lock_name)
                elif state == "stale":
                    result["stale_locks"].append(lock_name)
                else:
                    result["unknown_locks"].append(lock_name)
        except OSError:
            result["valid"] = False
    result["active_names"] = sorted(set(result["active_names"]))
    result["live_pids"] = sorted(set(result["live_pids"]))
    result["unknown_live_pids"] = sorted(set(result["unknown_live_pids"]))
    result["reused_pids"] = sorted(set(result["reused_pids"]))
    result["stale_pids"] = sorted(set(result["stale_pids"]))
    result["lock_names"] = sorted(set(result["lock_names"]))
    result["live_locks"] = sorted(set(result["live_locks"]))
    result["unknown_locks"] = sorted(set(result["unknown_locks"]))
    result["stale_locks"] = sorted(set(result["stale_locks"]))
    return result


def _public_runtime_metadata(runtime: dict[str, Any]) -> dict[str, Any]:
    return dict(runtime)


def _relation_for_runtime(canonical: dict[str, Any], other: dict[str, Any]) -> str:
    if not canonical["valid"] or not other["valid"]:
        return "INVALID"
    if canonical["exists"] and not other["exists"]:
        return "CANONICAL_ONLY"
    if other["exists"] and not canonical["exists"]:
        return "LEGACY_ONLY"
    if not canonical["exists"] and not other["exists"]:
        return "IDENTICAL"
    left = {
        key: canonical[key]
        for key in ("entry_count", "active_names", "live_pids", "live_locks")
    }
    right = {
        key: other[key]
        for key in ("entry_count", "active_names", "live_pids", "live_locks")
    }
    return "IDENTICAL" if left == right else "DIFFERENT"


def _paths_for_root(root: Path) -> AppDataPaths:
    return AppDataPaths(
        root=root,
        data=root / "data",
        logs=root / "logs",
        runtime=root / "runtime",
        config=root / "config",
        cache=root / "cache",
        components=root / "components",
        resolution_source="reconciliation",
    )


def _root_fingerprint(root: Path) -> str:
    records = _fingerprint_records(root)
    payload = json.dumps(records, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _fingerprint_records(root: Path) -> list[dict[str, Any]]:
    root = root.absolute()
    records: list[dict[str, Any]] = [
        {"kind": "root", "path": _path_key(root), "exists": root.exists()}
    ]
    if not root.exists():
        return records
    tracked: set[Path] = {
        STATE_PATHS[name]
        for name in PERSISTENT_STATE_NAMES
    }
    tracked.update(
        relative.parent
        for relative in tuple(tracked)
        if relative.parent != Path(".")
    )
    tracked.add(Path(LEGACY_INACTIVE_MARKER))
    tracked = {
        relative
        for relative in tracked
        if not any(
            parent != Path(".") and parent in tracked
            for parent in relative.parents
        )
    }
    for relative in sorted(tracked, key=lambda value: value.as_posix()):
        candidate = root / relative
        if not candidate.exists():
            records.append({"kind": "missing", "path": relative.as_posix()})
            continue
        if candidate.is_file() or candidate.is_symlink():
            records.append(_fingerprint_item(candidate, relative.as_posix()))
            continue
        try:
            for current, dirnames, filenames in os.walk(
                candidate,
                topdown=True,
                followlinks=False,
            ):
                current_path = Path(current)
                dirnames[:] = [
                    name for name in dirnames if name not in _IGNORED_FINGERPRINT_DIRS
                ]
                for name in sorted(dirnames + filenames, key=str.casefold):
                    path = current_path / name
                    relative_path = path.relative_to(root).as_posix()
                    records.append(_fingerprint_item(path, relative_path))
        except OSError:
            records.append({"kind": "walk_error", "path": relative.as_posix()})
    return sorted(records, key=lambda item: (str(item.get("path")), str(item.get("kind"))))


def _fingerprint_item(path: Path, relative: str) -> dict[str, Any]:
    try:
        stat = path.lstat()
    except OSError:
        return {"kind": "unreadable", "path": relative}
    item: dict[str, Any] = {
        "kind": "link" if path.is_symlink() else ("dir" if path.is_dir() else "file"),
        "path": relative,
        "size": int(stat.st_size),
        "mtime_ns": int(stat.st_mtime_ns),
    }
    if item["kind"] == "file" and not _is_secret_path(relative):
        if stat.st_size <= _HASH_LIMIT:
            item["sha256"] = _sha256_file(path)
    return item


def _is_secret_path(relative: str) -> bool:
    parts = [part.casefold() for part in Path(relative).parts]
    return len(parts) >= 2 and parts[-2:] == ["config", "secrets"] or (
        len(parts) >= 3 and parts[-3:-1] == ["config", "secrets"]
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return digest.hexdigest()


def _validate_backup(source: Path, target: Path) -> None:
    source_manifest = _content_manifest(source)
    target_manifest = _content_manifest(target)
    if source_manifest != target_manifest:
        raise ReconciliationError("backup validation failed")
    for name in PERSISTENT_STATE_NAMES:
        relative = STATE_PATHS[name]
        if (source / relative).exists() and not (target / relative).exists():
            raise ReconciliationError(f"backup is missing {relative.as_posix()}")


def _content_manifest(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    if not root.exists():
        return [{"kind": "missing", "path": "."}]
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirnames[:] = [name for name in dirnames if name not in _IGNORED_FINGERPRINT_DIRS]
        for name in sorted(dirnames + filenames, key=str.casefold):
            path = current_path / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                records.append({"kind": "link", "path": relative, "target": os.readlink(path)})
                continue
            if path.is_dir():
                records.append({"kind": "dir", "path": relative})
                continue
            item: dict[str, Any] = {"kind": "file", "path": relative}
            try:
                item["size"] = path.stat().st_size
                if _is_secret_path(relative):
                    item["sha256"] = None
                else:
                    item["sha256"] = _sha256_file(path)
            except OSError:
                item["size"] = None
                item["sha256"] = "unreadable"
            records.append(item)
    return sorted(records, key=lambda item: (str(item["path"]), str(item["kind"])))


def _same_path(left: Path, right: Path) -> bool:
    return _path_key(left) == _path_key(right)


def _same_physical_path(left: Path, right: Path) -> bool:
    try:
        return left.exists() and right.exists() and left.samefile(right)
    except (OSError, ValueError):
        return False


def _path_key(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path.absolute()))).casefold()


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
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _fsync_tree(child)
        _fsync_directory(path)
    else:
        _fsync_file(path)
