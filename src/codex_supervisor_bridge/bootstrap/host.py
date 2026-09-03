from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Mapping

from .codex_isolation import (
    SUPERVISOR_HOST_INSTANCE_ENV,
    ProcessInspector,
    ProcessObservation,
    ProcessSnapshotIndex,
)
from .paths import AppDataPaths
from .physical import (
    PHYSICAL_PATH_UNVERIFIED,
    SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH,
    PhysicalPathEvidence,
    PhysicalPathGuard,
    PhysicalPathVerificationError,
    current_package_identity,
)
from .process import ProcessManager

SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE = "SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE"
SUPERVISOR_HOST_ALREADY_RUNNING = "SUPERVISOR_HOST_ALREADY_RUNNING"
SUPERVISOR_HOST_IDENTITY_UNKNOWN = "SUPERVISOR_HOST_IDENTITY_UNKNOWN"
SUPERVISOR_HOST_AUTHORITY_ENV = "CODEX_SUPERVISOR_HOST_AUTHORITY"
SUPERVISOR_HOST_AUTHORITY_VALUE = "standalone-v1"
SUPERVISOR_HOST_IDENTITY_PATH_ENV = "CODEX_SUPERVISOR_HOST_IDENTITY_PATH"
HOST_IDENTITY_SCHEMA_VERSION = 1
HOST_IDENTITY_FILENAME = "host-identity.json"


class SupervisorHostOwnership(str, Enum):
    SUPERVISOR_HOST_MANAGED = "SUPERVISOR_HOST_MANAGED"
    DESKTOP_EXTERNAL = "DESKTOP_EXTERNAL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SupervisorHostIdentity:
    pid: int
    executable: str
    creation_time: str
    parent_pid: int | None
    parent_creation_time: str | None
    parent_executable: str | None
    ownership: SupervisorHostOwnership
    package_identity: str
    command_line_fingerprint: str | None = None
    host_instance_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "host_pid": self.pid,
            "host_executable": self.executable,
            "host_creation_time": self.creation_time,
            "host_parent_pid": self.parent_pid,
            "host_parent_creation_time": self.parent_creation_time,
            "host_parent_executable": self.parent_executable,
            "host_command_line_fingerprint": self.command_line_fingerprint,
            "host_ownership": self.ownership.value,
            "package_identity": self.package_identity,
            "host_instance_id": self.host_instance_id,
        }


@dataclass(frozen=True)
class SupervisorHostEvidence:
    identity: SupervisorHostIdentity | None
    requested_app_data_root: str
    physical_app_data_root: str | None
    requested_component_root: str
    physical_component_root: str | None
    requested_runtime_root: str
    physical_runtime_root: str | None
    requested_lcb_root: str
    physical_lcb_root: str | None
    host_identity_path: str
    host_identity_persisted: bool
    app_data: PhysicalPathEvidence
    components: PhysicalPathEvidence
    runtime: PhysicalPathEvidence
    lcb: PhysicalPathEvidence
    physical_root_verified: bool
    failure_code: str | None = None
    technical_detail: str | None = None

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "requested_app_data_root": self.requested_app_data_root,
            "physical_app_data_root": self.physical_app_data_root,
            "requested_component_root": self.requested_component_root,
            "physical_component_root": self.physical_component_root,
            "requested_runtime_root": self.requested_runtime_root,
            "physical_runtime_root": self.physical_runtime_root,
            "requested_lcb_root": self.requested_lcb_root,
            "physical_lcb_root": self.physical_lcb_root,
            "host_identity_path": self.host_identity_path,
            "host_identity_persisted": self.host_identity_persisted,
            "physical_root_verified": self.physical_root_verified,
            "failure_code": self.failure_code,
            "technical_detail": self.technical_detail,
            "path_evidence": {
                "app_data": self.app_data.as_dict(),
                "components": self.components.as_dict(),
                "runtime": self.runtime.as_dict(),
                "lcb": self.lcb.as_dict(),
            },
        }
        if self.identity is not None:
            payload.update(self.identity.as_dict())
        return payload


class SupervisorHostEnvironmentGuard:
    """Prove the host and its managed roots before any lifecycle mutation."""

    def __init__(
        self,
        paths: AppDataPaths,
        *,
        physical_guard: PhysicalPathGuard | None = None,
        process_inspector: ProcessInspector | None = None,
        environ: Mapping[str, str] | None = None,
        host_identity_path: str | Path | None = None,
    ) -> None:
        self.paths = paths
        self.physical_guard = physical_guard or PhysicalPathGuard()
        self.process_inspector = process_inspector or ProcessInspector()
        self.environ = environ if environ is not None else os.environ
        inherited_identity_path = self.environ.get(SUPERVISOR_HOST_IDENTITY_PATH_ENV, "").strip()
        self.host_identity_path = (
            Path(host_identity_path)
            if host_identity_path
            else Path(inherited_identity_path)
            if inherited_identity_path
            else paths.root / "runtime" / HOST_IDENTITY_FILENAME
        )

    def inspect(self) -> SupervisorHostEvidence:
        requested_app_data = self.paths.root
        requested_components = self.paths.root / "components"
        requested_runtime = self.paths.root / "runtime"
        requested_lcb = requested_components / "local-codex-bridge" / "2.1.3"
        evidence: dict[str, PhysicalPathEvidence] = {}
        failures: list[PhysicalPathVerificationError] = []
        try:
            evidence["app_data"] = self.physical_guard.verify_root(
                requested_app_data,
                role="app_data",
                require_directory=True,
            )
        except PhysicalPathVerificationError as exc:
            failures.append(exc)
            evidence["app_data"] = exc.evidence or self.physical_guard.inspect(requested_app_data)
        for role, path in (
            ("components", requested_components),
            ("runtime", requested_runtime),
            ("lcb", requested_lcb),
        ):
            try:
                # Every managed child must remain below the verified physical
                # AppData root, including when a junction redirects it.
                evidence[role] = self.physical_guard.verify_subpath(
                    path,
                    requested_app_data,
                    role=role,
                    require_directory=True,
                )
            except PhysicalPathVerificationError as exc:
                failures.append(exc)
                evidence[role] = exc.evidence or self.physical_guard.inspect(path)

        if self.paths.physical_root is not None and _path_key(self.paths.physical_root) != _path_key(
            self.paths.root
        ):
            failures.append(
                PhysicalPathVerificationError(
                    SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH,
                    "AppDataPaths selected a non-canonical physical alias",
                )
            )

        process_snapshot = self._capture_process_snapshot()
        observation = process_snapshot.get(os.getpid())
        identity = self._identity(
            observation=observation,
            process_snapshot=process_snapshot,
        )
        if identity is None:
            failures.append(
                PhysicalPathVerificationError(
                    SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
                    "Standalone Supervisor Host identity is unavailable",
                )
            )
        elif not _complete_host_identity(identity):
            failures.append(
                PhysicalPathVerificationError(
                    SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                    "Standalone Supervisor Host process identity is incomplete",
                )
            )
        elif identity.ownership != SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED:
            failures.append(
                PhysicalPathVerificationError(
                    SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
                    "host is running in a Desktop or unknown process ancestry",
                )
            )

        # Do not read persisted host state until every managed root has passed
        # the physical namespace check.  A Desktop-derived execution context
        # can redirect the same requested AppData path before the read occurs.
        persisted: dict[str, object] | None = None
        if not failures:
            try:
                persisted = self._read_persisted_identity()
            except PhysicalPathVerificationError as exc:
                failures.append(exc)
            else:
                if self._inherited_authority_requested():
                    if observation is None:
                        failures.append(
                            PhysicalPathVerificationError(
                                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                                "current Supervisor child identity is unavailable",
                            )
                        )
                    elif persisted is None:
                        failures.append(
                            PhysicalPathVerificationError(
                                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                                "inherited Host authority identity is missing",
                            )
                        )
                    else:
                        authority_error = self._validate_inherited_authority(
                            persisted,
                            observation,
                            process_snapshot=process_snapshot,
                        )
                        if authority_error is not None:
                            failures.append(authority_error)
                        else:
                            identity = self._identity(
                                observation=observation,
                                host_instance_id=str(persisted["host_instance_id"]),
                                process_snapshot=process_snapshot,
                            )
                else:
                    identity = self._identity(
                        persisted,
                        observation=observation,
                        process_snapshot=process_snapshot,
                    )
        if (
            persisted is not None
            and identity is not None
            and not self._inherited_authority_requested()
        ):
            persisted_observation = (
                process_snapshot.get(persisted["pid"])
                if isinstance(persisted.get("pid"), int)
                else None
            )
            if persisted_observation is not None and not _matches_persisted_identity(
                persisted,
                persisted_observation,
            ):
                # The old PID is alive but no longer represents the persisted
                # host. Treat it as stale PID reuse; ensure_identity() may
                # safely replace the record without touching that process.
                persisted_observation = None
            if persisted_observation is not None and persisted["pid"] != identity.pid:
                failures.append(
                    PhysicalPathVerificationError(
                        SUPERVISOR_HOST_ALREADY_RUNNING,
                        "another Supervisor Host owns the persisted host identity",
                    )
                )
        identity_persisted = bool(
            persisted
            and identity is not None
            and identity.host_instance_id
            and persisted.get("host_instance_id") == identity.host_instance_id
        )

        first_failure = failures[0] if failures else None
        return SupervisorHostEvidence(
            identity=identity,
            requested_app_data_root=str(requested_app_data),
            physical_app_data_root=_physical(evidence["app_data"]),
            requested_component_root=str(requested_components),
            physical_component_root=_physical(evidence["components"]),
            requested_runtime_root=str(requested_runtime),
            physical_runtime_root=_physical(evidence["runtime"]),
            requested_lcb_root=str(requested_lcb),
            physical_lcb_root=_physical(evidence["lcb"]),
            host_identity_path=str(self.host_identity_path),
            host_identity_persisted=identity_persisted,
            app_data=evidence["app_data"],
            components=evidence["components"],
            runtime=evidence["runtime"],
            lcb=evidence["lcb"],
            physical_root_verified=not failures
            and all(item.verified for item in evidence.values()),
            failure_code=first_failure.code if first_failure else None,
            technical_detail=str(first_failure) if first_failure else None,
        )

    def assert_ready(self) -> SupervisorHostEvidence:
        evidence = self.inspect()
        if not evidence.physical_root_verified:
            raise PhysicalPathVerificationError(
                evidence.failure_code or PHYSICAL_PATH_UNVERIFIED,
                evidence.technical_detail or "Supervisor Host physical root is not verified",
            )
        return evidence

    def prepare_app_data(self) -> SupervisorHostEvidence:
        """Create managed roots only after the parent physical root is proven."""

        # A first install has no bridge directory yet. Verify the nearest
        # existing parent and the host identity before creating any child.
        if self.paths.physical_root is not None and _path_key(self.paths.physical_root) != _path_key(
            self.paths.root
        ):
            raise PhysicalPathVerificationError(
                SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH,
                "AppDataPaths selected a non-canonical physical alias",
            )
        preflight = self.inspect()
        if not preflight.physical_root_verified:
            raise PhysicalPathVerificationError(
                preflight.failure_code or PHYSICAL_PATH_UNVERIFIED,
                preflight.technical_detail or "Supervisor Host preflight failed",
            )
        root = self.paths.root
        if root.exists():
            self.physical_guard.verify_root(root, role="app_data", require_directory=True)
        else:
            parent = _nearest_existing(root.parent)
            if parent is None:
                raise PhysicalPathVerificationError(
                    SUPERVISOR_APPDATA_PHYSICAL_ROOT_MISMATCH,
                    "no existing AppData parent can be verified before bootstrap",
                )
            self.physical_guard.verify_root(parent, role="app_data", require_directory=True)
        for role, path in (
            ("app_data", root),
            ("components", root / "components"),
            ("runtime", root / "runtime"),
            ("path", root / "data"),
            ("path", root / "logs"),
            ("path", root / "config"),
            ("path", root / "cache"),
        ):
            self.physical_guard.ensure_directory(path, role=role)
        return self.assert_ready()

    def process_manager(self) -> ProcessManager:
        return ProcessManager(
            self.paths.runtime,
            self.paths.logs,
            path_guard=self.physical_guard,
        )

    def _identity(
        self,
        persisted: dict[str, object] | None = None,
        *,
        observation: ProcessObservation | None = None,
        host_instance_id: str | None = None,
        process_snapshot: ProcessSnapshotIndex | None = None,
    ) -> SupervisorHostIdentity | None:
        process_snapshot = process_snapshot or self._capture_process_snapshot()
        observation = observation if observation is not None else process_snapshot.get(os.getpid())
        if observation is None:
            return None
        ownership = self._classify_ownership(
            observation,
            process_snapshot=process_snapshot,
        )
        if host_instance_id is None:
            host_instance_id = (
                str(persisted["host_instance_id"])
                if persisted
                and isinstance(persisted.get("host_instance_id"), str)
                and _matches_persisted_identity(persisted, observation)
                else None
            )
        return SupervisorHostIdentity(
            pid=observation.pid,
            executable=observation.executable,
            creation_time=observation.creation_time,
            parent_pid=observation.parent_pid,
            parent_creation_time=observation.parent_creation_time,
            parent_executable=observation.parent_executable,
            ownership=ownership,
            package_identity=current_package_identity(),
            command_line_fingerprint=observation.command_line_fingerprint,
            host_instance_id=host_instance_id,
        )

    def _read_persisted_identity(self) -> dict[str, object] | None:
        self.physical_guard.verify_subpath(
            self.host_identity_path,
            self.paths.root / "runtime",
            role="runtime",
        )
        if not self.host_identity_path.exists():
            return None
        try:
            payload = json.loads(self.host_identity_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        if payload.get("schema_version") != HOST_IDENTITY_SCHEMA_VERSION:
            return None
        if not isinstance(payload.get("host_instance_id"), str):
            return None
        if not isinstance(payload.get("pid"), int):
            return None
        if not isinstance(payload.get("creation_time"), str):
            return None
        if not isinstance(payload.get("executable"), str):
            return None
        return payload

    def _inherited_authority_requested(self) -> bool:
        return bool(
            self.environ.get(SUPERVISOR_HOST_INSTANCE_ENV, "").strip()
            and self.environ.get(SUPERVISOR_HOST_IDENTITY_PATH_ENV, "").strip()
        )

    def _validate_inherited_authority(
        self,
        persisted: Mapping[str, object],
        observation: ProcessObservation,
        *,
        process_snapshot: ProcessSnapshotIndex | None = None,
    ) -> PhysicalPathVerificationError | None:
        process_snapshot = process_snapshot or self._capture_process_snapshot()
        expected_instance = self.environ.get(SUPERVISOR_HOST_INSTANCE_ENV, "").strip()
        persisted_instance = persisted.get("host_instance_id")
        if not expected_instance or persisted_instance != expected_instance:
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "inherited Host instance identity does not match persisted authority",
            )
        if _path_key(Path(self.environ[SUPERVISOR_HOST_IDENTITY_PATH_ENV])) != _path_key(
            self.host_identity_path
        ):
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "inherited Host identity path does not match the managed path",
            )
        if persisted.get("host_ownership") not in {
            None,
            SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED.value,
        }:
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
                "inherited Host authority is not Supervisor-managed",
            )
        authority_pid = persisted.get("pid")
        if not isinstance(authority_pid, int) or authority_pid <= 0:
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "inherited Host authority PID is invalid",
            )
        if authority_pid == observation.pid:
            if _matches_persisted_identity(persisted, observation):
                return None
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "current process does not match the persisted Host authority",
            )
        authority = process_snapshot.get(authority_pid)
        if authority is None or not _matches_persisted_identity(persisted, authority):
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "inherited Host authority process identity is stale or unknown",
            )
        if not (
            observation.parent_pid == authority.pid
            and observation.parent_creation_time == authority.creation_time
            and _same_executable(observation.parent_executable, authority.executable)
        ):
            return PhysicalPathVerificationError(
                SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
                "Supervisor child is not a direct child of the verified Host authority",
            )
        return None

    def _capture_process_snapshot(self) -> ProcessSnapshotIndex:
        return ProcessSnapshotIndex.from_observations(self.process_inspector.snapshot())

    def _classify_ownership(
        self,
        observation: ProcessObservation,
        *,
        process_snapshot: ProcessSnapshotIndex | None = None,
    ) -> SupervisorHostOwnership:
        process_snapshot = process_snapshot or self._capture_process_snapshot()
        current = observation
        for _ in range(32):
            parent_pid = current.parent_pid
            if parent_pid is None or parent_pid == current.pid or parent_pid <= 1:
                return SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED
            parent = process_snapshot.get(parent_pid)
            if parent is None or not _parent_observation_matches(current, parent):
                return SupervisorHostOwnership.UNKNOWN
            if _is_desktop_process(parent.executable):
                return SupervisorHostOwnership.DESKTOP_EXTERNAL
            current = parent
        return SupervisorHostOwnership.UNKNOWN


class StandaloneSupervisorHost:
    """The lifecycle authority for Supervisor-owned resources."""

    def __init__(
        self,
        *,
        paths: AppDataPaths | None = None,
        physical_guard: PhysicalPathGuard | None = None,
        process_inspector: ProcessInspector | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.paths = paths or AppDataPaths.from_environment(environ=environ)
        self.path_guard = physical_guard or PhysicalPathGuard()
        self.environment_guard = SupervisorHostEnvironmentGuard(
            self.paths,
            physical_guard=self.path_guard,
            process_inspector=process_inspector,
            environ=environ,
        )

    @property
    def host_identity_path(self) -> Path:
        return self.environment_guard.host_identity_path

    def preflight(self) -> SupervisorHostEvidence:
        return self.environment_guard.inspect()

    def assert_ready(self) -> SupervisorHostEvidence:
        return self.environment_guard.assert_ready()

    def prepare_app_data(self) -> SupervisorHostEvidence:
        evidence = self.environment_guard.prepare_app_data()
        self.ensure_identity(evidence=evidence)
        return self.preflight()

    def ensure_identity(
        self,
        *,
        evidence: SupervisorHostEvidence | None = None,
    ) -> SupervisorHostIdentity:
        """Persist one authority identity before lifecycle work begins.

        A Supervisor service child may inherit an already verified authority
        from the standalone host.  It reuses that instance identity but never
        overwrites the authority record owned by its parent.
        """

        evidence = evidence or self.assert_ready()
        current = evidence.identity
        if current is None or current.ownership != SupervisorHostOwnership.SUPERVISOR_HOST_MANAGED:
            raise PhysicalPathVerificationError(
                SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE,
                "Standalone Supervisor Host identity is unavailable or unsafe",
            )
        process_snapshot = self.environment_guard._capture_process_snapshot()
        persisted = self.environment_guard._read_persisted_identity()
        if self.environment_guard._inherited_authority_requested():
            if persisted is None:
                raise PhysicalPathVerificationError(
                    SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                    "inherited Host authority identity is missing",
                )
            inherited_error = self.environment_guard._validate_inherited_authority(
                persisted,
                ProcessObservation(
                    pid=current.pid,
                    creation_time=current.creation_time,
                    executable=current.executable,
                    command_line_fingerprint=current.command_line_fingerprint,
                    parent_pid=current.parent_pid,
                    parent_creation_time=current.parent_creation_time,
                    parent_executable=current.parent_executable,
                ),
                process_snapshot=process_snapshot,
            )
            if inherited_error is not None:
                raise inherited_error
            host_instance_id = str(persisted["host_instance_id"])
            return replace(current, host_instance_id=host_instance_id)
        if persisted is not None and not _matches_persisted_identity(
            persisted,
            ProcessObservation(
                pid=current.pid,
                creation_time=current.creation_time,
                executable=current.executable,
                command_line_fingerprint=current.command_line_fingerprint,
                parent_pid=current.parent_pid,
                parent_creation_time=current.parent_creation_time,
                parent_executable=current.parent_executable,
            ),
        ):
            old = (
                process_snapshot.get(persisted["pid"])
                if isinstance(persisted.get("pid"), int)
                else None
            )
            if old is not None and _matches_persisted_identity(persisted, old):
                raise PhysicalPathVerificationError(
                    SUPERVISOR_HOST_ALREADY_RUNNING,
                    "another Standalone Supervisor Host process is still running",
                )

        host_instance_id = (
            str(persisted["host_instance_id"])
            if persisted is not None
            and _matches_persisted_identity(
                persisted,
                ProcessObservation(
                    pid=current.pid,
                    creation_time=current.creation_time,
                    executable=current.executable,
                    parent_pid=current.parent_pid,
                    parent_creation_time=current.parent_creation_time,
                    parent_executable=current.parent_executable,
                    command_line_fingerprint=current.command_line_fingerprint,
                ),
            )
            else f"csb-host-{uuid.uuid4()}"
        )
        identity = SupervisorHostIdentity(
            pid=current.pid,
            executable=current.executable,
            creation_time=current.creation_time,
            parent_pid=current.parent_pid,
            parent_creation_time=current.parent_creation_time,
            parent_executable=current.parent_executable,
            ownership=current.ownership,
            package_identity=current.package_identity,
            command_line_fingerprint=current.command_line_fingerprint,
            host_instance_id=host_instance_id,
        )
        self.path_guard.ensure_directory(self.host_identity_path.parent, role="runtime")
        self.path_guard.before_write(self.host_identity_path, role="runtime")
        descriptor, temporary = self.path_guard.create_temp_file(
            self.host_identity_path.parent,
            prefix=f".{HOST_IDENTITY_FILENAME}.",
            suffix=".tmp",
            role="runtime",
        )
        payload = {
            "schema_version": HOST_IDENTITY_SCHEMA_VERSION,
            **identity.as_dict(),
            "pid": identity.pid,
            "creation_time": identity.creation_time,
            "executable": identity.executable,
            "parent_pid": identity.parent_pid,
            "parent_creation_time": identity.parent_creation_time,
            "parent_executable": identity.parent_executable,
            "command_line_fingerprint": identity.command_line_fingerprint,
        }
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            self.path_guard.replace(temporary, self.host_identity_path, role="runtime")
        finally:
            self.path_guard.remove(temporary, role="runtime")
        return identity

    def process_manager(self) -> ProcessManager:
        self.ensure_identity()
        return self.environment_guard.process_manager()


def main(argv: list[str] | None = None) -> None:
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--preflight" not in argv:
        host = StandaloneSupervisorHost()
        identity = host.prepare_app_data().identity
        if identity is None or not identity.host_instance_id:
            raise PhysicalPathVerificationError(
                SUPERVISOR_HOST_IDENTITY_UNKNOWN,
                "Standalone Supervisor Host identity could not be persisted",
            )
        from codex_supervisor_bridge.mcp.server import main as supervisor_main

        authority_keys = (
            SUPERVISOR_HOST_AUTHORITY_ENV,
            SUPERVISOR_HOST_INSTANCE_ENV,
            SUPERVISOR_HOST_IDENTITY_PATH_ENV,
        )
        previous_environment = {key: os.environ.get(key) for key in authority_keys}
        os.environ.update(
            {
                SUPERVISOR_HOST_AUTHORITY_ENV: SUPERVISOR_HOST_AUTHORITY_VALUE,
                SUPERVISOR_HOST_INSTANCE_ENV: identity.host_instance_id,
                SUPERVISOR_HOST_IDENTITY_PATH_ENV: str(host.host_identity_path),
            }
        )
        try:
            supervisor_main(argv)
        finally:
            for key, value in previous_environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        return
    parser = argparse.ArgumentParser(description="Run the standalone Supervisor Host preflight")
    parser.add_argument("--json", action="store_true", dest="json_output")
    argv.remove("--preflight")
    args = parser.parse_args(argv)
    host = StandaloneSupervisorHost()
    evidence = host.preflight()
    payload = evidence.as_dict()
    if args.json_output:
        print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    else:
        print(
            "STANDALONE SUPERVISOR HOST READY"
            if evidence.physical_root_verified
            else "STANDALONE SUPERVISOR HOST INCOMPLETE"
        )
        print(f"physical_root_verified={evidence.physical_root_verified}")
        if evidence.failure_code:
            print(f"failure_code={evidence.failure_code}")


def _physical(evidence: PhysicalPathEvidence) -> str | None:
    return evidence.physical_path or evidence.nearest_existing_physical_path


def _is_desktop_process(value: str | None) -> bool:
    if not value:
        return False
    normalized = value.replace("/", "\\").casefold()
    name = Path(value).name.casefold()
    if name in {"chatgpt", "chatgpt.exe", "codex", "codex.exe"}:
        return True
    return "\\windowsapps\\" in normalized and (
        "openai.codex_" in normalized or "\\codex\\" in normalized
    )


def _path_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(value))).casefold()


def _same_executable(left: str | None, right: str | None) -> bool:
    return bool(
        left
        and right
        and os.path.normcase(str(left)) == os.path.normcase(str(right))
    )


def _parent_observation_matches(
    child: ProcessObservation,
    parent: ProcessObservation,
) -> bool:
    return bool(
        child.parent_pid == parent.pid
        and child.parent_creation_time
        and child.parent_creation_time != "unknown"
        and child.parent_creation_time == parent.creation_time
        and parent.creation_time
        and parent.creation_time != "unknown"
        and _same_executable(child.parent_executable, parent.executable)
    )


def _matches_persisted_identity(
    persisted: Mapping[str, object],
    observation: ProcessObservation,
) -> bool:
    return bool(
        isinstance(persisted.get("pid"), int)
        and persisted["pid"] == observation.pid
        and persisted.get("creation_time") == observation.creation_time
        and isinstance(persisted.get("executable"), str)
        and os.path.normcase(str(persisted["executable"]))
        == os.path.normcase(observation.executable)
        and (
            "command_line_fingerprint" not in persisted
            or persisted.get("command_line_fingerprint")
            == observation.command_line_fingerprint
        )
        and persisted.get("parent_pid") == observation.parent_pid
        and persisted.get("parent_creation_time") == observation.parent_creation_time
        and os.path.normcase(str(persisted.get("parent_executable") or ""))
        == os.path.normcase(observation.parent_executable or "")
    )


def _complete_host_identity(identity: SupervisorHostIdentity) -> bool:
    return bool(
        identity.pid > 0
        and identity.creation_time
        and identity.creation_time != "unknown"
        and identity.executable
        and identity.command_line_fingerprint
        and identity.parent_pid is not None
        and identity.parent_creation_time
        and identity.parent_creation_time != "unknown"
        and identity.parent_executable
    )


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


if __name__ == "__main__":
    main()


__all__ = [
    "StandaloneSupervisorHost",
    "HOST_IDENTITY_FILENAME",
    "SUPERVISOR_HOST_ALREADY_RUNNING",
    "SUPERVISOR_HOST_AUTHORITY_ENV",
    "SUPERVISOR_HOST_AUTHORITY_VALUE",
    "SUPERVISOR_HOST_IDENTITY_PATH_ENV",
    "SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE",
    "SUPERVISOR_HOST_IDENTITY_UNKNOWN",
    "SupervisorHostEnvironmentGuard",
    "SupervisorHostEvidence",
    "SupervisorHostIdentity",
    "SupervisorHostOwnership",
]
