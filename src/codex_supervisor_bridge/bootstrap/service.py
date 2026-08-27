from __future__ import annotations

import shlex
import sys
from pathlib import Path

from .configuration import AppConfig, ConfigStore
from .doctor import Doctor, DoctorOptions
from .models import BootstrapStatus, HealthStatus, RepairAction
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
        self.doctor = doctor or Doctor(paths=self.paths, config_store=self.config_store)
        self.repair = repair or RepairService(paths=self.paths, config_store=self.config_store, doctor=self.doctor)
        self.process_manager = process_manager or self.repair.process_manager

    def status(self, *, project_directory: Path | None = None) -> BootstrapStatus:
        doctor = self.doctor.run(DoctorOptions(project_directory=project_directory))
        config = self.config_store.load().config
        project = project_directory or config.basic.project_directory
        return BootstrapStatus(
            status=doctor.status,
            summary=_summary(doctor.status),
            project_directory=str(project) if project else None,
            selected_profile=_profile(config),
            doctor=doctor,
        )

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
        )
        state = self.process_manager.start(spec)
        start_action = RepairAction(
            action="start_supervisor",
            status=HealthStatus.READY if state.status == "RUNNING" else HealthStatus.UNAVAILABLE,
            message="Local development environment started." if state.status == "RUNNING" else "Local development environment could not start.",
            advanced=state.as_dict(),
        )
        result.repairs.append(start_action)
        for name, command in config.advanced.process_commands.items():
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
        final.repairs = [*result.repairs]
        return final


def _summary(status: HealthStatus) -> str:
    return {
        HealthStatus.READY: "开发环境已就绪。",
        HealthStatus.DEGRADED: "开发环境可以使用，但有项目需要修复或一次性设置。",
        HealthStatus.UNAVAILABLE: "开发环境暂时不可用。",
        HealthStatus.REPAIRING: "正在修复开发环境。",
    }[status]


def _profile(config: AppConfig) -> str:
    return {
        "automatic": "hybrid",
        "web_first": "direct",
        "codex_first": "codex_supervised",
    }[config.basic.development_style.value]
