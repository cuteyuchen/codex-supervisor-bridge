from __future__ import annotations

import re
import shlex
import shutil
import sys
from pathlib import Path

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.capabilities import CapabilityResolver

from .configuration import AppConfig, CommandPolicy, ConfigStore, DevelopmentStyle
from .devspace import DevSpaceBootstrap
from .doctor import Doctor, DoctorOptions
from .local_codex import LocalCodexBridgeBootstrap
from .models import BootstrapStatus, DoctorStatus, HealthStatus, RepairAction
from .paths import AppDataPaths
from .process import ManagedProcessSpec, ProcessManager
from .repair import RepairService


class BootstrapService:
    def __init__(
        self,
        *,
        paths: AppDataPaths | None = None,
        config_store: ConfigStore | None = None,
        doctor: Doctor | None = None,
        repair: RepairService | None = None,
        process_manager: ProcessManager | None = None,
    ) -> None:
        self.paths = paths or AppDataPaths.from_environment()
        self.config_store = config_store or ConfigStore(paths=self.paths)
        self.process_manager = process_manager or ProcessManager(self.paths.runtime, self.paths.logs)
        self.doctor = doctor or Doctor(
            paths=self.paths,
            config_store=self.config_store,
            process_manager=self.process_manager,
        )
        self.repair = repair or RepairService(
            paths=self.paths,
            config_store=self.config_store,
            doctor=self.doctor,
            process_manager=self.process_manager,
        )

    def status(self, *, project_directory: Path | None = None) -> BootstrapStatus:
        doctor = self.doctor.run(DoctorOptions(project_directory=project_directory))
        config = self.config_store.load().config
        project = project_directory or config.basic.project_directory
        return BootstrapStatus(
            status=doctor.status,
            summary=_summary(doctor.status),
            project_directory=str(project) if project else None,
            selected_profile=_profile(config, doctor),
            doctor=doctor,
        )

    def configure(
        self,
        *,
        project_directory: Path | None = None,
        development_style: DevelopmentStyle | None = None,
        local_command_policy: CommandPolicy | None = None,
        allow_chatgpt_codex_delegation: bool | None = None,
        automatic_git_commit: bool | None = None,
        automatic_pull_request: bool | None = None,
        local_codex_repository: Path | None = None,
        node_executable: str | None = None,
    ) -> BootstrapStatus:
        config = self.config_store.load().config
        if project_directory is not None:
            config.basic.project_directory = project_directory.expanduser().resolve()
        if development_style is not None:
            config.basic.development_style = development_style
        if local_command_policy is not None:
            config.basic.local_command_policy = local_command_policy
        if allow_chatgpt_codex_delegation is not None:
            config.basic.allow_chatgpt_codex_delegation = allow_chatgpt_codex_delegation
        if automatic_git_commit is not None:
            config.basic.automatic_git_commit = automatic_git_commit
        if automatic_pull_request is not None:
            config.basic.automatic_pull_request = automatic_pull_request
        if local_codex_repository is not None:
            config.advanced.local_codex_repository = local_codex_repository.expanduser().resolve()
        if node_executable is not None:
            config.advanced.executable_paths["node"] = node_executable
        self.config_store.save(config)
        return self.status(project_directory=project_directory or config.basic.project_directory)

    def repair_and_status(self, *, project_directory: Path | None = None) -> BootstrapStatus:
        before = self.doctor.run(DoctorOptions(project_directory=project_directory))
        repairs = self.repair.repair(before, project_directory=project_directory)
        after = self.status(project_directory=project_directory)
        after.repairs = repairs
        return after

    def start(self, *, project_directory: Path | None = None) -> BootstrapStatus:
        result = self.repair_and_status(project_directory=project_directory)
        config = self.config_store.load().config
        if result.status == HealthStatus.UNAVAILABLE:
            return result
        port = config.advanced.ports.get("supervisor")
        if port is None:
            return result
        spec = ManagedProcessSpec(
            name="supervisor",
            command=[
                sys.executable,
                "-m",
                "codex_supervisor_bridge.mcp.server",
                "--database",
                str(config.advanced.sqlite_path or self.paths.database),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
            ],
            startup_timeout=config.advanced.startup_timeout_seconds,
            shutdown_timeout=config.advanced.shutdown_timeout_seconds,
            readiness_probe=lambda: _readiness_marker_present(
                self.paths.logs / "supervisor.log"
            ),
        )
        try:
            state = self.process_manager.start(spec)
        except (OSError, ValueError) as exc:
            result.repairs.append(
                RepairAction(
                    action="start_supervisor",
                    status=HealthStatus.UNAVAILABLE,
                    message="Local development environment could not start.",
                    requires_user_action=False,
                    advanced={"technical_detail": str(exc)},
                )
            )
            return result
        readiness = _read_readiness_marker(state.log_path)
        start_status = (
            HealthStatus.READY
            if state.status == "RUNNING" and (readiness is None or readiness == HealthStatus.READY)
            else HealthStatus.DEGRADED
            if state.status == "RUNNING"
            else HealthStatus.UNAVAILABLE
        )
        start_action = RepairAction(
            action="start_supervisor",
            status=start_status,
            message=(
                "Local development environment started."
                if state.status == "RUNNING" and readiness == HealthStatus.READY
                else "Local development environment started with a repair needed."
                if state.status == "RUNNING"
                else "Local development environment could not start."
            ),
            advanced={**state.as_dict(), "readiness": readiness.value if readiness else None},
        )
        result.repairs.append(start_action)
        devspace_executable = config.advanced.executable_paths.get("devspace", "devspace")
        workspace_health = result.doctor.component("Local workspace")
        if workspace_health is not None and workspace_health.status != HealthStatus.READY:
            result.repairs.append(
                RepairAction(
                    action="start_process:devspace",
                    status=workspace_health.status,
                    message=workspace_health.user_message,
                    requires_user_action=True,
                    advanced={**workspace_health.advanced, "executable": devspace_executable},
                )
            )
        elif _find_executable(devspace_executable):
            devspace = DevSpaceBootstrap.from_app_data(
                self.paths,
                port=config.advanced.ports.get("devspace", port),
                project_directory=project_directory or config.basic.project_directory,
                executable=devspace_executable,
            )
            try:
                component = self.process_manager.start(
                    devspace.process_spec(
                        startup_timeout=config.advanced.startup_timeout_seconds,
                        shutdown_timeout=config.advanced.shutdown_timeout_seconds,
                    )
                )
                result.repairs.append(
                    RepairAction(
                        action="start_process:devspace",
                        status=HealthStatus.READY if component.status == "RUNNING" else HealthStatus.UNAVAILABLE,
                        message="Local workspace started." if component.status == "RUNNING" else "Local workspace could not start.",
                        advanced=component.as_dict(),
                    )
                )
            except (OSError, ValueError) as exc:
                result.repairs.append(
                    RepairAction(
                        action="start_process:devspace",
                        status=HealthStatus.UNAVAILABLE,
                        message="Local workspace could not start.",
                        advanced={"technical_detail": str(exc)},
                    )
                )
        control_health = result.doctor.component("Codex control")
        lcb_command = config.advanced.process_commands.get("local_codex_bridge")
        launch: list[str] | None = None
        if lcb_command and lcb_command.strip():
            launch = shlex.split(lcb_command, posix=False)
        elif config.advanced.local_codex_repository:
            try:
                launch = LocalCodexBridgeBootstrap.from_repository(
                    config.advanced.local_codex_repository,
                    node_executable=config.advanced.executable_paths.get("node", "node"),
                ).config.launch_command
            except FileNotFoundError as exc:
                launch = None
                result.repairs.append(
                    RepairAction(
                        action="agent_session:local_codex_bridge",
                        status=HealthStatus.UNAVAILABLE,
                        message="Codex control needs an update or build.",
                        requires_user_action=True,
                        advanced={"technical_detail": str(exc)},
                    )
                )
        if launch:
            if control_health is not None and control_health.status != HealthStatus.READY:
                result.repairs.append(
                    RepairAction(
                        action="agent_session:local_codex_bridge",
                        status=control_health.status,
                        message=control_health.user_message,
                        requires_user_action=True,
                        advanced={**dict(control_health.advanced), "launch_command": launch},
                    )
                )
            else:
                result.repairs.append(
                    RepairAction(
                        action="agent_session:local_codex_bridge",
                        status=HealthStatus.READY,
                        message="Codex control is ready.",
                        requires_user_action=False,
                        advanced={
                            "managed_by": "agent_session_manager",
                            "daemon": False,
                            "launch_command": launch,
                        },
                    )
                )
        for name, command in config.advanced.process_commands.items():
            if name == "local_codex_bridge":
                continue
            if name == "supervisor" or not command.strip():
                continue
            try:
                argv = shlex.split(command, posix=False)
                component = self.process_manager.start(
                    ManagedProcessSpec(
                        name=name,
                        command=argv,
                        startup_timeout=config.advanced.startup_timeout_seconds,
                        shutdown_timeout=config.advanced.shutdown_timeout_seconds,
                    )
                )
            except (OSError, ValueError) as exc:
                result.repairs.append(
                    RepairAction(
                        action=f"start_process:{name}",
                        status=HealthStatus.UNAVAILABLE,
                        message="A local development component could not start.",
                        requires_user_action=False,
                        advanced={"technical_detail": str(exc)},
                    )
                )
                continue
            result.repairs.append(
                RepairAction(
                    action=f"start_process:{name}",
                    status=HealthStatus.READY if component.status == "RUNNING" else HealthStatus.UNAVAILABLE,
                    message="Local development component started." if component.status == "RUNNING" else "Local development component could not start.",
                    advanced=component.as_dict(),
                )
            )
        final = self.status(project_directory=project_directory)
        if readiness is not None and readiness != HealthStatus.READY:
            final.status = readiness
            final.summary = _summary(readiness)
        final.repairs = [*result.repairs]
        return final


def _summary(status: HealthStatus) -> str:
    return {
        HealthStatus.READY: "开发环境已就绪。",
        HealthStatus.DEGRADED: "开发环境可以使用，但有项目需要修复或一次性设置。",
        HealthStatus.UNAVAILABLE: "开发环境暂时不可用。",
        HealthStatus.REPAIRING: "正在修复开发环境。",
    }[status]


def _profile(config: AppConfig, doctor: DoctorStatus) -> str:
    style = config.basic.development_style.value
    if style == "web_first":
        return "direct"
    if style == "codex_first":
        return "codex_supervised"
    health: dict[str, BackendHealth] = {}
    aliases = {
        "devspace": "Local workspace",
        "local_codex_bridge": "Codex control",
        "kandev": "Fallback workspace",
        "control_plane": "Fallback control",
        "github": "GitHub",
    }
    for name, label in aliases.items():
        item = doctor.component(label)
        if item is None:
            continue
        mapped = {
            HealthStatus.READY: BackendHealthStatus.READY,
            HealthStatus.DEGRADED: BackendHealthStatus.DEGRADED,
            HealthStatus.UNAVAILABLE: BackendHealthStatus.UNAVAILABLE,
            HealthStatus.REPAIRING: BackendHealthStatus.DEGRADED,
        }[item.status]
        health[name] = BackendHealth(
            capability=name,
            status=mapped,
            user_message=item.user_message,
            repairable=item.repairable,
            technical_detail=str(item.advanced.get("technical_detail", "")),
        )
    return CapabilityResolver(health).resolve().profile


def _find_executable(value: str) -> str | None:
    return value if Path(value).is_file() else shutil.which(value)


def _read_readiness_marker(log_path: Path | None) -> HealthStatus | None:
    if log_path is None or not log_path.exists():
        return None
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in reversed(text.splitlines()):
        if "SUPERVISOR_READY" not in line:
            continue
        match = re.search(r"status=([A-Z_]+)", line)
        if match is None:
            return None
        try:
            return HealthStatus(match.group(1))
        except ValueError:
            return None
    return None


def _readiness_marker_present(log_path: Path | None) -> bool:
    return _read_readiness_marker(log_path) is not None
