from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .component_registry import ManagedComponentRegistry
from .configuration import AppConfig, ConfigStore
from .devspace import DevSpaceBootstrap
from .doctor import Doctor, DoctorOptions
from .installer import ComponentInstaller
from .models import DoctorStatus, HealthStatus, RepairAction
from .paths import AppDataPaths
from .ports import PortAllocator
from .process import ProcessManager
from .secrets import MemorySecretStore, SecretStore, WindowsDpapiSecretStore


class RepairService:
    """Perform only bounded local repairs; unsafe external authorization stays manual."""

    def __init__(
        self,
        *,
        paths: AppDataPaths | None = None,
        config_store: ConfigStore | None = None,
        doctor: Doctor | None = None,
        process_manager: ProcessManager | None = None,
        port_allocator: PortAllocator | None = None,
        secret_store: SecretStore | None = None,
        installer: ComponentInstaller | None = None,
        registry: ManagedComponentRegistry | None = None,
        auto_install: bool = False,
    ) -> None:
        self.paths = paths or AppDataPaths.from_environment()
        self.config_store = config_store or ConfigStore(paths=self.paths)
        self.doctor = doctor or Doctor(paths=self.paths, config_store=self.config_store)
        self.process_manager = process_manager or ProcessManager(self.paths.runtime, self.paths.logs)
        self.port_allocator = port_allocator or PortAllocator()
        self.secret_store = secret_store or self._default_secret_store()
        self.registry = registry or ManagedComponentRegistry()
        self.installer = installer or ComponentInstaller(
            self.paths.components,
            trusted_manifests=self.registry.manifests(),
        )
        self.auto_install = auto_install

    def repair(self, status: DoctorStatus | None = None, *, project_directory: Path | None = None) -> list[RepairAction]:
        status = status or self.doctor.run()
        actions: list[RepairAction] = []
        if not self.paths.data.exists() or not self.paths.logs.exists() or not self.paths.runtime.exists() or not self.paths.config.exists() or not self.paths.cache.exists():
            self.paths.ensure_directories()
            actions.append(RepairAction(action="repair_data_directory", status=HealthStatus.READY, message="Application data is ready."))

        loaded = self.config_store.load()
        config = loaded.config
        if project_directory is not None:
            config.basic.project_directory = project_directory.expanduser().resolve()
        if config.advanced.sqlite_path is None:
            config.advanced.sqlite_path = self.paths.database
        selected_ports: set[int] = set()
        for name, action, message in (
            ("devspace", "allocate_devspace_port", "Local workspace port selected."),
            ("supervisor", "allocate_local_port", "Local connection port selected."),
        ):
            preferred = config.advanced.ports.get(name)
            try:
                lease = self.port_allocator.reserve(preferred, excluded=selected_ports)
            except OSError:
                actions.append(
                    RepairAction(
                        action=action,
                        status=HealthStatus.UNAVAILABLE,
                        message="No local connection port is available.",
                        requires_user_action=True,
                    )
                )
                continue
            selected_ports.add(lease.port)
            changed = preferred != lease.port
            config.advanced.ports[name] = lease.port
            lease.release()
            if changed:
                actions.append(RepairAction(action=action, status=HealthStatus.READY, message=message))
        self.config_store.save(config)
        self._write_mcp_config(config)
        actions.append(RepairAction(action="generate_mcp_config", status=HealthStatus.READY, message="Local connection settings are ready."))
        project = project_directory or config.basic.project_directory
        if config.advanced.ports.get("devspace"):
            devspace_entrypoint = config.advanced.executable_paths.get(
                "devspace_entrypoint"
            )
            devspace = DevSpaceBootstrap.from_app_data(
                self.paths,
                port=config.advanced.ports["devspace"],
                project_directory=project,
                executable=config.advanced.executable_paths.get("devspace", "devspace"),
                entrypoint=devspace_entrypoint,
            )
            devspace.write_config()
            devspace.prepare_auth(self.secret_store)
            actions.append(RepairAction(action="generate_workspace_config", status=HealthStatus.READY, message="Local workspace settings are ready."))

        for process in self.process_manager.statuses():
            if process.status in {"STALE", "CRASHED"}:
                repaired = self.process_manager.repair_stale(process.name)
                actions.append(RepairAction(action=f"repair_process:{process.name}", status=HealthStatus.READY, message="Stopped process state was recovered.", advanced=repaired.as_dict()))
            elif process.status == "UNKNOWN":
                actions.append(RepairAction(action=f"repair_process:{process.name}", status=HealthStatus.DEGRADED, message="A process may still be running; inspect before restarting.", requires_user_action=True, advanced=process.as_dict()))
        if project is None:
            actions.append(RepairAction(action="select_project_directory", status=HealthStatus.DEGRADED, message="Select a project directory to continue.", requires_user_action=True))
        actions.extend(self._component_install_plans(status, project_directory=project_directory))
        return actions

    def _component_install_plans(
        self,
        status: DoctorStatus,
        *,
        project_directory: Path | None,
    ) -> list[RepairAction]:
        actions: list[RepairAction] = []
        node_required = self.doctor.run(
            DoctorOptions(
                project_directory=project_directory,
                require_node=True,
                check_optional_components=False,
            )
        )
        node_item = node_required.component("Node.js")
        if node_item is not None and node_item.status != HealthStatus.READY:
            actions.append(self._install_action("nodejs", node_item))
        for name, label in (
            ("devspace", "Local workspace"),
            ("local-codex-bridge", "Codex control"),
        ):
            item = status.component(label)
            if item is None or item.status == HealthStatus.READY:
                continue
            actions.append(self._install_action(name, item))
        return actions

    def _install_action(
        self,
        name: str,
        component: object,
    ) -> RepairAction:
        manifest = self.registry.manifest(name)
        if not self.auto_install:
            plan = self.installer.plan(manifest)
            return RepairAction(
                action=f"install_component:{name}",
                status=getattr(component, "status"),
                message=str(getattr(component, "user_message")),
                requires_user_action=True,
                advanced={
                    "install_plan": plan.model_dump(mode="json"),
                    "verification_strategy": self.registry.verification_strategy(name),
                },
            )
        result = self.installer.install(manifest)
        if result.status in {"INSTALLED", "ALREADY_INSTALLED"} and result.installed_path:
            self._register_managed_paths(name, result.installed_path)
        return RepairAction(
            action=f"install_component:{name}",
            status=(
                HealthStatus.READY
                if result.status in {"INSTALLED", "ALREADY_INSTALLED"}
                else HealthStatus.DEGRADED
            ),
            message=(
                "Local development environment is ready."
                if result.status in {"INSTALLED", "ALREADY_INSTALLED"}
                else "Local development environment needs a repair."
            ),
            requires_user_action=result.status not in {"INSTALLED", "ALREADY_INSTALLED"},
            advanced={
                "install_result": result.model_dump(mode="json"),
                "verification_strategy": self.registry.verification_strategy(name),
                "repairable": bool(getattr(component, "repairable", False)),
            },
        )

    def _register_managed_paths(self, name: str, installed_path: Path) -> None:
        loaded = self.config_store.load()
        config = loaded.config
        if name == "nodejs":
            node_exe = installed_path / "node.exe"
            if node_exe.is_file():
                config.advanced.executable_paths["node"] = str(node_exe)
        elif name == "devspace":
            entrypoint = installed_path / "dist" / "cli.js"
            node_exe = config.advanced.executable_paths.get("node")
            if entrypoint.is_file():
                config.advanced.executable_paths["devspace_entrypoint"] = str(entrypoint)
            if node_exe:
                config.advanced.executable_paths["devspace"] = node_exe
        elif name == "local-codex-bridge":
            entrypoint = installed_path / "dist" / "src" / "index.js"
            if entrypoint.is_file():
                config.advanced.local_codex_repository = installed_path
        self.config_store.save(config)

    def _write_mcp_config(self, config: AppConfig) -> None:
        port = config.advanced.ports.get("supervisor")
        if port is None:
            return
        payload = {
            "mcpServers": {
                "codex-supervisor-bridge": {
                    "url": f"http://127.0.0.1:{port}/mcp",
                }
            }
        }
        self.paths.config.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix="mcp-", suffix=".tmp", dir=self.paths.config)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.paths.generated_mcp_config)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _default_secret_store(self) -> SecretStore:
        import platform

        if platform.system() == "Windows":
            return WindowsDpapiSecretStore(self.paths.config / "secrets")
        return MemorySecretStore()
