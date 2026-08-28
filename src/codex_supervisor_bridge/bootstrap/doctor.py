from __future__ import annotations

import platform
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from codex_supervisor_bridge import __version__

from .codex_runtime import CodexReadinessDetector
from .configuration import AppConfig, ConfigStore
from .devspace import (
    DEVSPACE_SUPPORTED_VERSION_RANGE,
    DEVSPACE_TESTED_VERSIONS,
    DevSpaceVersionCompatibility,
)
from .models import ComponentHealth, DoctorStatus, HealthStatus
from .paths import AppDataPaths
from .ports import PortAllocator
from .process import ProcessManager
from .remote import SecureRemoteAccessConfig, SecureRemoteAccessValidator

CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]

NODE_PROFILE_B_MIN_MAJOR = 24
NODE_PROFILE_B_MAX_EXCLUSIVE_MAJOR = 27


@dataclass(frozen=True)
class DoctorOptions:
    project_directory: Path | None = None
    require_node: bool = False
    check_optional_components: bool = True


class Doctor:
    """Collect structured health without exposing provider details in normal UX."""

    def __init__(
        self,
        *,
        paths: AppDataPaths | None = None,
        config_store: ConfigStore | None = None,
        executable_finder: Callable[[str], str | None] = shutil.which,
        command_runner: CommandRunner | None = None,
        bind_checker: Callable[[str], bool] | None = None,
        port_allocator: PortAllocator | None = None,
        process_manager: ProcessManager | None = None,
    ) -> None:
        self.paths = paths or AppDataPaths.from_environment()
        self.config_store = config_store or ConfigStore(paths=self.paths)
        self._find = executable_finder
        self._run = command_runner or self._run_command
        self._can_bind = bind_checker or self._check_bind
        self._ports = port_allocator or PortAllocator()
        self._process_manager = process_manager

    def run(self, options: DoctorOptions | None = None) -> DoctorStatus:
        options = options or DoctorOptions()
        loaded = self.config_store.load()
        config = loaded.config
        project = options.project_directory or config.basic.project_directory
        components = [
            self._platform(),
            self._python(),
            self._executable("Git", "git", "git --version"),
            self._codex(config, workspace=project),
            self._node(config, required=options.require_node),
            self._supervisor(),
            self._project(project),
            self._data_directory(),
            self._local_network(),
            self._port(config),
            self._github(project),
            self._secure_remote(config),
            self._shell(),
        ]
        if options.check_optional_components:
            components.extend(
                [
                    self._workspace(),
                    self._codex_control(config),
                    self._executable("Fallback workspace", "kandev", "kandev --version"),
                    self._executable("Fallback control", "codex-control-plane-mcp", "codex-control-plane-mcp --version"),
                ]
            )
        if loaded.status == "DEGRADED":
            components.append(
                ComponentHealth(
                    capability="Configuration",
                    status=HealthStatus.DEGRADED,
                    repairable=True,
                    user_message="Settings are invalid; safe defaults are active.",
                    recommended_action="repair_configuration",
                    advanced={"technical_detail": loaded.error or "invalid configuration"},
                )
            )
        return DoctorStatus(status=_overall_status(components), components=components)

    def _platform(self) -> ComponentHealth:
        system = platform.system()
        if system == "Windows":
            return ComponentHealth(
                capability="Windows",
                status=HealthStatus.READY,
                user_message="Windows integration is ready.",
                advanced={"provider": "windows", "version": platform.version(), "release": platform.release()},
            )
        return ComponentHealth(
            capability="Windows",
            status=HealthStatus.DEGRADED,
            repairable=False,
            user_message="Portable checks are available; Windows integration needs a Windows machine.",
            advanced={"provider": system, "version": platform.version(), "release": platform.release()},
        )

    def _python(self) -> ComponentHealth:
        return ComponentHealth(
            capability="Python",
            status=HealthStatus.READY,
            user_message="Python is ready.",
            advanced={"executable": sys.executable, "version": platform.python_version()},
        )

    def _supervisor(self) -> ComponentHealth:
        process = self._process_manager.health("supervisor") if self._process_manager else None
        if process is not None and process.status != "RUNNING":
            crashed = process.status in {"CRASHED", "STALE", "UNKNOWN"}
            return ComponentHealth(
                capability="Supervisor Bridge",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message=(
                    "Development environment needs a restart."
                    if crashed
                    else "Development environment needs to start."
                ),
                recommended_action=(
                    "restart_supervisor" if crashed else "start_supervisor"
                ),
                advanced=process.as_dict(),
            )
        return ComponentHealth(
            capability="Supervisor Bridge",
            status=HealthStatus.READY,
            user_message="Development environment is ready.",
            advanced={
                "provider": "codex-supervisor-bridge",
                "version": __version__,
                "executable": sys.executable,
                "process": process.as_dict() if process else None,
            },
        )

    def _data_directory(self) -> ComponentHealth:
        paths = (self.paths.data, self.paths.logs, self.paths.runtime, self.paths.config, self.paths.cache)
        missing = [str(path) for path in paths if not path.exists()]
        if not missing:
            status = HealthStatus.READY
            message = "Application data is ready."
            repairable = False
        else:
            status = HealthStatus.DEGRADED
            message = "Application data will be prepared automatically."
            repairable = True
        return ComponentHealth(
            capability="Application data",
            status=status,
            repairable=repairable,
            user_message=message,
            recommended_action="repair_data_directory" if repairable else None,
            advanced={"paths": {"data": str(self.paths.data), "logs": str(self.paths.logs), "runtime": str(self.paths.runtime), "config": str(self.paths.config), "cache": str(self.paths.cache), "missing": missing}},
        )

    def _project(self, project: Path | None) -> ComponentHealth:
        if project is None:
            return ComponentHealth(
                capability="Project directory",
                status=HealthStatus.DEGRADED,
                repairable=False,
                user_message="Choose a project directory to begin.",
                recommended_action="select_project_directory",
                advanced={"path": None},
            )
        resolved = project.expanduser().resolve()
        if not resolved.is_dir():
            return ComponentHealth(
                capability="Project directory",
                status=HealthStatus.UNAVAILABLE,
                repairable=False,
                user_message="The selected project directory is unavailable.",
                recommended_action="select_project_directory",
                advanced={"path": str(resolved), "technical_detail": "directory does not exist"},
            )
        git_dir = resolved / ".git"
        return ComponentHealth(
            capability="Project directory",
            status=HealthStatus.READY if git_dir.exists() else HealthStatus.DEGRADED,
            repairable=False,
            user_message="Local project is ready." if git_dir.exists() else "Local project is available but is not a Git repository.",
            recommended_action=None if git_dir.exists() else "open_git_project",
            advanced={"path": str(resolved), "git_repository": git_dir.exists()},
        )

    def _local_network(self) -> ComponentHealth:
        ready = self._can_bind("127.0.0.1")
        return ComponentHealth(
            capability="Local network",
            status=HealthStatus.READY if ready else HealthStatus.UNAVAILABLE,
            repairable=False,
            user_message="Local network is ready." if ready else "Local network binding is unavailable.",
            advanced={"bind_host": "127.0.0.1", "technical_detail": "loopback bind probe"},
        )

    def _port(self, config: AppConfig) -> ComponentHealth:
        preferred = config.advanced.ports.get("supervisor")
        try:
            lease = self._ports.reserve(preferred)
        except OSError as exc:
            return ComponentHealth(
                capability="Local port",
                status=HealthStatus.UNAVAILABLE,
                repairable=True,
                user_message="A local connection port needs repair.",
                recommended_action="allocate_local_port",
                advanced={"preferred": preferred, "technical_detail": _redact(str(exc))},
            )
        port = lease.port
        lease.release()
        return ComponentHealth(
            capability="Local port",
            status=HealthStatus.READY,
            user_message="Local connection is ready.",
            advanced={"host": "127.0.0.1", "port": port, "preferred": preferred},
        )

    def _github(self, project: Path | None) -> ComponentHealth:
        gh = self._find("gh")
        if gh is None:
            return ComponentHealth(
                capability="GitHub",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message="GitHub connection needs setup.",
                recommended_action="connect_github",
                advanced={"executable": None, "project": str(project) if project else None},
            )
        result = self._run([gh, "auth", "status"])
        if result.returncode == 0:
            return ComponentHealth(
                capability="GitHub",
                status=HealthStatus.READY,
                user_message="GitHub is ready.",
                advanced={"executable": gh, "version": self._version(gh, [gh, "--version"])},
            )
        return ComponentHealth(
            capability="GitHub",
            status=HealthStatus.DEGRADED,
            repairable=False,
            user_message="GitHub needs sign-in.",
            recommended_action="connect_github",
            advanced={"executable": gh, "technical_detail": "authentication check failed"},
        )

    def _secure_remote(self, config: AppConfig) -> ComponentHealth:
        detail = config.advanced.tunnel_detail
        remote = SecureRemoteAccessConfig(
            public_url=detail.get("public_url"),
            bind_host=detail.get("bind_host", "127.0.0.1"),
            bind_port=_int_or_none(detail.get("bind_port")),
            auth_secret_ref=detail.get("auth_secret_ref"),
            session_identity=detail.get("session_identity"),
        )
        errors = SecureRemoteAccessValidator.validate(remote)
        if not errors:
            return ComponentHealth(
                capability="ChatGPT connection",
                status=HealthStatus.READY,
                user_message="ChatGPT connection is ready.",
                advanced={"public_url": remote.public_url, "bind_host": remote.bind_host, "bind_port": remote.bind_port, "session_identity": remote.session_identity},
            )
        return ComponentHealth(
            capability="ChatGPT connection",
            status=HealthStatus.DEGRADED,
            repairable=False,
            user_message="ChatGPT connection needs one-time setup.",
            recommended_action="connect_chatgpt",
            advanced={"technical_detail": errors, "bind_host": remote.bind_host},
        )

    def _shell(self) -> ComponentHealth:
        bash = self._find("bash")
        if bash is None:
            return ComponentHealth(
                capability="Shell support",
                status=HealthStatus.UNAVAILABLE,
                repairable=False,
                user_message="A compatible local shell is required for the workspace.",
                recommended_action="install_git_bash_or_wsl",
                advanced={"provider": "bash", "executable": None},
            )
        return ComponentHealth(
            capability="Shell support",
            status=HealthStatus.READY,
            user_message="Local shell is ready.",
            advanced={"provider": "bash", "executable": bash},
        )

    def _workspace(self) -> ComponentHealth:
        managed = self._managed_component("devspace")
        config = self.config_store.load().config
        if managed is not None:
            entrypoint = managed / "dist" / "cli.js"
            node = self._resolve_executable(
                config.advanced.executable_paths.get("node", "node")
            )
            if node is not None and entrypoint.is_file():
                version_result = self._run([node, str(entrypoint), "--version"])
                version = (
                    _first_line(version_result.stdout or version_result.stderr)
                    if version_result.returncode == 0
                    else None
                )
                compatible = DevSpaceVersionCompatibility.is_supported(version)
                status = (
                    HealthStatus.READY
                    if compatible
                    else HealthStatus.DEGRADED
                )
                return ComponentHealth(
                    capability="Local workspace",
                    status=status,
                    repairable=not compatible,
                    user_message=(
                        "Local workspace is ready."
                        if compatible
                        else "Local workspace needs a compatible release."
                    ),
                    recommended_action=(
                        None if compatible else "repair_local_workspace"
                    ),
                    advanced={
                        "managed": True,
                        "version": version,
                        "launch_command": [node, str(entrypoint), "serve"],
                        "upstream_compatibility": {
                            "tested_versions": list(DEVSPACE_TESTED_VERSIONS),
                            "supported_version_range": DEVSPACE_SUPPORTED_VERSION_RANGE,
                            "compatible": compatible,
                        },
                    },
                )
        result = self._executable("Local workspace", "devspace", "devspace --version")
        version = result.advanced.get("version")
        result.advanced["upstream_compatibility"] = {
            "tested_versions": list(DEVSPACE_TESTED_VERSIONS),
            "supported_version_range": DEVSPACE_SUPPORTED_VERSION_RANGE,
            "compatible": DevSpaceVersionCompatibility.is_supported(version),
        }
        if result.status == HealthStatus.READY and not DevSpaceVersionCompatibility.is_supported(version):
            result.status = HealthStatus.DEGRADED
            result.repairable = False
            result.user_message = "Local workspace needs a compatible release."
            result.recommended_action = "repair_local_workspace"
        if result.status == HealthStatus.READY and self._find("bash") is None:
            result.status = HealthStatus.DEGRADED
            result.repairable = False
            result.user_message = "Local workspace needs Git Bash or WSL."
            result.recommended_action = "install_git_bash_or_wsl"
            result.advanced["shell"] = None
        return result

    def _codex_control(self, config: AppConfig) -> ComponentHealth:
        repository = config.advanced.local_codex_repository
        if repository is None:
            managed = self._managed_component("local-codex-bridge")
            if managed is not None:
                repository = managed
            else:
                return self._executable(
                    "Codex control",
                    "local-codex-bridge",
                    "local-codex-bridge --version",
                )

        resolved = repository.expanduser().resolve()
        entrypoint = resolved / "dist" / "src" / "index.js"
        node_value = config.advanced.executable_paths.get("node", "node")
        node = self._resolve_executable(node_value) or self._resolve_executable("node")
        if node is None:
            return ComponentHealth(
                capability="Codex control",
                status=HealthStatus.UNAVAILABLE,
                repairable=True,
                user_message="Codex control needs Node.js or a runtime repair.",
                recommended_action="repair_codex_control",
                advanced={
                    "provider": "local-codex-bridge",
                    "repository": str(resolved),
                    "executable": None,
                    "technical_detail": "node executable not found",
                },
            )
        if not entrypoint.is_file():
            return ComponentHealth(
                capability="Codex control",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message="Codex control needs an update or build.",
                recommended_action="repair_codex_control",
                advanced={
                    "provider": "local-codex-bridge",
                    "repository": str(resolved),
                    "entrypoint": str(entrypoint),
                    "technical_detail": "dist/src/index.js not found",
                },
            )
        version_result = self._run([node, "--version"])
        node_version = _first_line(version_result.stdout or version_result.stderr) if version_result.returncode == 0 else None
        match = re.search(r"(\d+)", node_version or "")
        node_major = int(match.group(1)) if match else None
        if (
            node_major is None
            or node_major < NODE_PROFILE_B_MIN_MAJOR
            or node_major >= NODE_PROFILE_B_MAX_EXCLUSIVE_MAJOR
        ):
            return ComponentHealth(
                capability="Codex control",
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message=(
                    "Codex control needs a compatible Node.js runtime."
                ),
                recommended_action="repair_codex_control",
                advanced={
                    "provider": "local-codex-bridge",
                    "repository": str(resolved),
                    "entrypoint": str(entrypoint),
                    "node_version": node_version,
                    "node_required_range": ">=24, <27",
                    "technical_detail": (
                        "Profile B common runtime floor is Node.js >=24 and <27"
                    ),
                },
            )
        return ComponentHealth(
            capability="Codex control",
            status=HealthStatus.READY,
            user_message="Codex control is ready.",
            advanced={
                "provider": "local-codex-bridge",
                "repository": str(resolved),
                "entrypoint": str(entrypoint),
                "launch_command": [node, str(entrypoint)],
                "node_version": node_version,
            },
        )

    def _codex(self, config: AppConfig, *, workspace: Path | None) -> ComponentHealth:
        configured = config.advanced.executable_paths.get("codex")
        detector = CodexReadinessDetector(
            finder=self._find,
            runner=lambda command, **kwargs: self._run(command),
        )
        readiness = detector.probe(
            executable=configured or "codex",
            workspace=workspace,
        )
        advanced = readiness.model_dump(mode="json")
        return ComponentHealth(
            capability="Codex",
            status=readiness.status,
            repairable=readiness.status != HealthStatus.READY,
            user_message=readiness.user_message,
            recommended_action="repair_codex" if readiness.status != HealthStatus.READY else None,
            advanced=advanced,
        )

    def _node(self, config: AppConfig, *, required: bool) -> ComponentHealth:
        configured = config.advanced.executable_paths.get("node")
        if configured:
            resolved = self._resolve_executable(configured)
            if resolved is not None:
                result = self._executable("Node.js", configured, "node --version")
            else:
                managed = self._managed_component("nodejs")
                if managed is not None:
                    node = managed / "node.exe"
                    if node.is_file():
                        version = self._run([str(node), "--version"])
                        if version.returncode == 0:
                            return ComponentHealth(
                                capability="Node.js",
                                status=HealthStatus.READY,
                                user_message="Node.js is ready.",
                                advanced={
                                    "managed": True,
                                    "executable": str(node),
                                    "version": _first_line(
                                        version.stdout or version.stderr
                                    ),
                                },
                            )
                result = self._executable("Node.js", configured, "node --version")
        else:
            managed = self._managed_component("nodejs")
            if managed is not None:
                node = managed / "node.exe"
                if node.is_file():
                    version = self._run([str(node), "--version"])
                    if version.returncode == 0:
                        return ComponentHealth(
                            capability="Node.js",
                            status=HealthStatus.READY,
                            user_message="Node.js is ready.",
                            advanced={
                                "managed": True,
                                "executable": str(node),
                                "version": _first_line(
                                    version.stdout or version.stderr
                                ),
                            },
                        )
            result = self._executable("Node.js", "node", "node --version")
        if result.status == HealthStatus.UNAVAILABLE and not required:
            return ComponentHealth(
                capability="Node.js",
                status=HealthStatus.READY,
                user_message="Node.js is not required for the selected setup.",
                advanced={"required": False, "executable": None},
            )
        return result

    def _resolve_executable(self, value: str) -> str | None:
        if Path(value).is_file():
            return value
        return self._find(value)

    def _managed_component(self, name: str) -> Path | None:
        import json

        pointer = self.paths.components / name / "current.json"
        try:
            payload = json.loads(pointer.read_text(encoding="utf-8"))
            path = Path(str(payload["path"]))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None
        if not path.is_dir():
            return None
        marker = path / ".codex-supervisor-installed.json"
        try:
            marker_payload = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        if marker_payload.get("name") != name:
            return None
        return path

    def _executable(
        self,
        label: str,
        command: str,
        version_command: str,
        *,
        auth_hint: bool = False,
    ) -> ComponentHealth:
        executable = command if Path(command).is_file() else self._find(command)
        if executable is None:
            return ComponentHealth(
                capability=label,
                status=HealthStatus.UNAVAILABLE,
                repairable=True,
                user_message=f"{label} needs installation or repair." if not auth_hint else "Codex needs installation or sign-in.",
                recommended_action="repair_codex" if auth_hint else "repair_environment",
                advanced={"executable": command, "technical_detail": "executable not found"},
            )
        result = self._run([executable, *version_command.split()[1:]])
        if result.returncode != 0:
            return ComponentHealth(
                capability=label,
                status=HealthStatus.DEGRADED,
                repairable=True,
                user_message=f"{label} needs repair." if not auth_hint else "Codex needs sign-in or runtime repair.",
                recommended_action="repair_codex" if auth_hint else "repair_environment",
                advanced={"executable": executable, "technical_detail": "version command failed"},
            )
        return ComponentHealth(
            capability=label,
            status=HealthStatus.READY,
            user_message=f"{label} is ready." if not auth_hint else "Codex is ready.",
            advanced={"executable": executable, "version": _first_line(result.stdout or result.stderr), "runtime_readiness": "version_and_launch_verified"},
        )

    @staticmethod
    def _run_command(command: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            return subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(command, 1, "", _redact(str(exc)))

    @staticmethod
    def _check_bind(host: str) -> bool:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind((host, 0))
        except OSError:
            return False
        finally:
            sock.close()
        return True

    def _version(self, executable: str, command: list[str]) -> str | None:
        result = self._run(command)
        return _first_line(result.stdout or result.stderr) if result.returncode == 0 else None


def _overall_status(components: list[ComponentHealth]) -> HealthStatus:
    statuses = {item.status for item in components}
    if HealthStatus.UNAVAILABLE in statuses:
        return HealthStatus.DEGRADED
    if HealthStatus.DEGRADED in statuses:
        return HealthStatus.DEGRADED
    return HealthStatus.READY


def _first_line(value: str) -> str:
    return _redact(value.strip().splitlines()[0] if value.strip() else "unknown")


def _redact(value: str) -> str:
    lowered = value.lower()
    for marker in ("bearer ", "token=", "access_token=", "refresh_token="):
        index = lowered.find(marker)
        if index >= 0:
            return value[:index] + marker + "[redacted]"
    return value[:500]


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
