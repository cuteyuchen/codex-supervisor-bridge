from __future__ import annotations

import argparse
import ipaddress
import json
import sys
from pathlib import Path

import anyio
from mcp.server import MCPServer

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.bootstrap import BootstrapService, CommandPolicy, DevelopmentStyle
from codex_supervisor_bridge.bootstrap.configuration import ConfigStore
from codex_supervisor_bridge.bootstrap.devspace_auth import DevSpaceLocalOAuthDriver
from codex_supervisor_bridge.bootstrap.doctor import DoctorOptions
from codex_supervisor_bridge.bootstrap.paths import AppDataPaths
from codex_supervisor_bridge.bootstrap.secrets import MemorySecretStore, WindowsDpapiSecretStore
from codex_supervisor_bridge.config import Settings
from codex_supervisor_bridge.integrations.codex_control_client import CodexControlAdapter
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.integrations.control_plane_agent import ControlPlaneAgentBackend
from codex_supervisor_bridge.integrations.devspace_client import DevSpaceWorkspaceAdapter
from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.memory.backend_binding import (
    list_runtime_affinity_bindings,
)
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_facade import (
    CodexCoordinatorFacade,
    CodexSemanticFacade,
)
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator
from codex_supervisor_bridge.supervisor.runtime import (
    ProfileReadiness,
    RuntimeComposition,
    lcb_environment,
    lcb_launch_from_config,
)
from codex_supervisor_bridge.supervisor.runtime_resolver import RuntimeResolver

from .checkpoint_tools import register_checkpoint_tools
from .codex_tools import register_codex_tools
from .direct_workspace_tools import register_direct_workspace_tools
from .execution_tools import register_execution_tools
from .kandev_tools import register_kandev_tools
from .tools import register_memory_tools

SERVER_INSTRUCTIONS = """
You are connected to Codex Supervisor Bridge, the durable task memory and
supervision surface for local software development. Chat history is not the
source of truth. Resume from canonical task/context state.

Before a task mutation, read the current task/context and use its latest
revision as expected_revision. If a tool reports STALE_CONTEXT, do not retry
with the old revision. Re-read canonical state first.

ACTIVE decisions and constraints are current instructions. Search may return
SUPERSEDED historical records; never treat them as current truth. Latest user
intent and ACTIVE HARD constraints outrank plans and agent-local choices.

A task has an execution mode:
- DIRECT: ChatGPT Web may be the workspace writer.
- HYBRID: ChatGPT and Codex may take turns writing through explicit handoff.
- CODEX_SUPERVISED: Codex owns mutating implementation work while ChatGPT
  supervises and reviews.

A supervised worktree has only one active writer by default. Read
get_task_execution_state before write ownership changes. Workspace mutations
must be fenced by both current task revision and writer_epoch. Never let a
stale ChatGPT request or Codex operation reclaim a newer writer lease.

HYBRID defaults to MANUAL_ONLY delegation. Do not decide that the user granted
ongoing automatic Codex delegation unless set_codex_delegation_policy records
that explicit user choice. A one-time explicit instruction to hand bounded work
to Codex may authorize only that handoff.

Backend names, ports, worker processes, SQLite paths, native thread IDs, and
similar details are implementation/diagnostic concerns. Normal user interaction
should describe capabilities such as Local workspace, Codex, and GitHub
rather than asking the user to select infrastructure backends.

Codex implementation remains plan-gated. start_codex_plan is read-only Plan
Mode; import_codex_plan creates a local DRAFT; approve_task_plan is the
Supervisor gate; execute_codex_approved_plan re-checks that the remote
latestPlan matches the locally approved plan before workspace-write is allowed.
A Codex implementation must also own the CODEX workspace writer lease once the
execution facade is enforcing leases end-to-end.

Use collect_codex_checkpoint to compress current legacy Control Plane progress
into a bounded HEARTBEAT, PROGRESS, or GATE checkpoint. Checkpoints are
Supervisor task progress, not permanent Codex-thread memory. Raw reasoning or
token deltas are not supervisor memory.

Use soft_steer_codex only for a local correction while the current plan remains
valid. Architecture or scope changes use the P6 hard-replan flow: record the
new user intent, snapshot existing work, interrupt/quiesce the current writer,
classify KEEP/MODIFY/DROP, and create/review a new plan. Do not revive a
superseded plan after an intent change.

Direct local workspace tools will be exposed only through a constrained
Supervisor facade after the WorkspaceBackend adapter is available. Do not
assume arbitrary raw filesystem or system shell access. Existing Kandev and
Codex Control Plane integrations are backend implementations/fallbacks, not
permanent Supervisor Core ownership boundaries.

Direct command execution is also constrained by the Supervisor facade. It is
workspace-bound, revision/writer fenced, bounded in output and duration, and
defaults to ASK. Dangerous commands fail closed; an UNKNOWN command outcome
requires reconciliation before another mutation.
""".strip()


def create_mcp_server(
    service: MemoryService,
    *,
    kandev: KandevCoordinator | None = None,
    codex: CodexCoordinator | None = None,
    agent_facade: CodexSemanticFacade | None = None,
    checkpoints: CheckpointService | None = None,
    direct_workspace: DirectWorkspaceCoordinator | None = None,
) -> MCPServer:
    server = MCPServer(
        "codex-supervisor-bridge",
        title="Codex Supervisor Bridge",
        description="Persistent memory and supervision bridge for ChatGPT and Codex.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    register_memory_tools(server, service)
    register_execution_tools(server, service)
    if direct_workspace is None:
        direct_workspace = DirectWorkspaceCoordinator(
            service,
            lambda: DevSpaceWorkspaceAdapter(),
        )
    register_direct_workspace_tools(server, direct_workspace)
    if kandev is not None:
        register_kandev_tools(server, kandev)
    if agent_facade is not None:
        register_codex_tools(server, agent_facade)
        checkpoint_backend = getattr(agent_facade, "checkpoint_backend", None)
        if checkpoints is None and checkpoint_backend is not None:
            checkpoints = CheckpointService(service, agent_backend=checkpoint_backend)
        if checkpoints is not None:
            register_checkpoint_tools(server, service, checkpoints)
    elif codex is not None:
        register_codex_tools(server, CodexCoordinatorFacade(codex))
        register_checkpoint_tools(
            server,
            service,
            CheckpointService(
                service,
                codex,
                agent_backend=ControlPlaneAgentBackend(codex.adapter_factory),
            ),
        )
    return server


def build_parser(settings: Settings | None = None) -> argparse.ArgumentParser:
    settings = settings or Settings.from_env()
    parser = argparse.ArgumentParser(description="Run Codex Supervisor Bridge MCP server")
    parser.add_argument(
        "command",
        nargs="?",
        choices=("configure", "doctor", "start", "status", "repair"),
        help="Bootstrap command; omit to run the MCP server",
    )
    parser.add_argument("--json", action="store_true", dest="json_output", help="Render structured output")
    parser.add_argument("--advanced", action="store_true", help="Include technical diagnostics")
    parser.add_argument("--project", type=Path, default=None, help="Project directory for bootstrap commands")
    parser.add_argument(
        "--style",
        choices=[item.value for item in DevelopmentStyle],
        default=None,
        help="Default development style for configure",
    )
    parser.add_argument(
        "--command-policy",
        choices=[item.value for item in CommandPolicy],
        default=None,
        help="Local command policy for configure",
    )
    parser.add_argument(
        "--allow-codex-delegation",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Allow ChatGPT to delegate to Codex automatically",
    )
    parser.add_argument(
        "--auto-commit",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Automatically create Git commits",
    )
    parser.add_argument(
        "--auto-pr",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Automatically create draft pull requests",
    )
    parser.add_argument(
        "--local-codex-repository",
        type=Path,
        default=None,
        help="Local-Codex-Bridge repository path for configure",
    )
    parser.add_argument(
        "--node",
        default=None,
        help="Node.js executable path for configure",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=settings.database_path,
        help="SQLite database path (default: SUPERVISOR_DB_PATH or user data directory)",
    )
    parser.add_argument(
        "--transport",
        choices=("streamable-http", "stdio"),
        default="streamable-http",
        help="MCP transport (default: streamable-http)",
    )
    parser.add_argument(
        "--host",
        default=settings.host,
        help="HTTP bind host (default: 127.0.0.1; keep local when using a secure tunnel)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=settings.port,
        help="HTTP bind port (default: 8765)",
    )
    parser.add_argument(
        "--mcp-path",
        default="/mcp",
        help="Streamable HTTP endpoint path (default: /mcp)",
    )
    parser.add_argument(
        "--kandev-mcp-url",
        default=settings.kandev_mcp_url,
        help="Kandev external MCP endpoint (advanced fallback; default local endpoint)",
    )
    parser.add_argument(
        "--devspace-mcp-url",
        default=settings.devspace_mcp_url,
        help="DevSpace workspace MCP endpoint (advanced; default local endpoint)",
    )
    parser.add_argument(
        "--codex-control-command",
        default=settings.codex_control_command,
        help="Codex Control Plane executable (advanced fallback)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is not None:
        bootstrap = BootstrapService(auto_install=True)
        if args.command in {"doctor", "status"}:
            result = bootstrap.status(project_directory=args.project)
        elif args.command == "repair":
            result = bootstrap.repair_and_status(project_directory=args.project)
        elif args.command == "configure":
            result = bootstrap.configure(
                project_directory=args.project,
                development_style=DevelopmentStyle(args.style) if args.style else None,
                local_command_policy=CommandPolicy(args.command_policy) if args.command_policy else None,
                allow_chatgpt_codex_delegation=args.allow_codex_delegation,
                automatic_git_commit=args.auto_commit,
                automatic_pull_request=args.auto_pr,
                local_codex_repository=args.local_codex_repository,
                node_executable=args.node,
            )
        else:
            result = bootstrap.start(project_directory=args.project)
        payload = result.advanced_view() if args.advanced else result.user_view()
        if args.json_output:
            print(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
        else:
            print(result.summary)
            for item in result.doctor.components:
                print(f"{item.status.value:11} {item.capability}: {item.user_message}")
        return
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.mcp_path.startswith("/"):
        parser.error("--mcp-path must start with '/'")
    if not args.kandev_mcp_url.strip():
        parser.error("--kandev-mcp-url must not be empty")
    if not args.devspace_mcp_url.strip():
        parser.error("--devspace-mcp-url must not be empty")
    if not args.codex_control_command.strip():
        parser.error("--codex-control-command must not be empty")
    if args.transport != "stdio" and not _is_loopback_host(args.host):
        parser.error("HTTP MCP must bind to loopback; use a secure HTTPS tunnel for remote access")

    service = MemoryService(args.database)
    app_paths = AppDataPaths.from_environment()
    secret_store = (
        WindowsDpapiSecretStore(app_paths.config / "secrets")
        if sys.platform == "win32"
        else MemorySecretStore()
    )
    devspace_auth = DevSpaceLocalOAuthDriver()

    def authenticated_devspace_factory() -> DevSpaceWorkspaceAdapter:
        return DevSpaceWorkspaceAdapter(
            args.devspace_mcp_url,
            transport_factory=lambda: devspace_auth.http_transport(
                mcp_url=args.devspace_mcp_url,
                secret_store=secret_store,
            ),
        )

    config = ConfigStore(paths=app_paths).load().config
    bootstrap = BootstrapService(
        paths=app_paths,
        config_store=ConfigStore(paths=app_paths),
    )
    doctor = bootstrap.doctor.run(
        DoctorOptions(project_directory=config.basic.project_directory)
    )
    forced_binding = _resolve_startup_binding(service)
    selection = RuntimeResolver(
        _doctor_health(doctor),
        development_style=config.basic.development_style.value,
        task_binding=forced_binding,
    ).resolve()
    launch_command: list[str] | None = None
    repository = config.advanced.local_codex_repository
    if repository is not None:
        try:
            launch_command = lcb_launch_from_config(
                repository=repository,
                node_executable=config.advanced.executable_paths.get("node", "node"),
            )
        except FileNotFoundError:
            launch_command = None
    if selection.profile == "lightweight" and launch_command:
        composition = RuntimeComposition.profile_b(
            service,
            launch_command=launch_command,
            env=lcb_environment(app_data_root=app_paths.root),
            workspace_factory=authenticated_devspace_factory,
        )
    elif selection.binding_forced and selection.profile == "lightweight":
        raise RuntimeError(
            "STARTUP_RECONCILIATION_REQUIRED: the bound task needs Profile B, "
            "but Local-Codex-Bridge cannot be launched"
        )
    else:
        composition = RuntimeComposition.profile_a(
            service,
            adapter_factory=lambda: CodexControlAdapter.stdio(
                args.codex_control_command,
                env={"CODEX_MCP_EXECUTION_MODE": "client"},
            ),
            kandev_adapter_factory=lambda: KandevAdapter(args.kandev_mcp_url),
        )
    composition.codex_readiness = _doctor_health(doctor).get("codex")
    kandev = KandevCoordinator(
        service,
        lambda: KandevAdapter(args.kandev_mcp_url),
    )
    direct_workspace = DirectWorkspaceCoordinator(
        service,
        composition.workspace_factory,
        backend_name=composition.workspace_backend,
    )

    async def serve() -> None:
        await composition.start()
        readiness = await composition.readiness()
        _emit_readiness_marker(readiness)
        facade = composition.agent_facade(service)
        server = create_mcp_server(
            service,
            kandev=kandev,
            agent_facade=facade,
            checkpoints=composition.checkpoint_service,
            direct_workspace=direct_workspace,
        )
        try:
            if args.transport == "stdio":
                await server.run_stdio_async()
            else:
                await server.run_streamable_http_async(
                    host=args.host,
                    port=args.port,
                    streamable_http_path=args.mcp_path,
                    json_response=True,
                    stateless_http=True,
                )
        finally:
            await composition.shutdown()
            service.close()

    anyio.run(serve)


def _doctor_health(doctor: object) -> dict[str, BackendHealth]:
    """Map Doctor components onto provider-neutral capability health."""
    from codex_supervisor_bridge.bootstrap.models import HealthStatus

    aliases = {
        "devspace": "Local workspace",
        "local_codex_bridge": "Codex control",
        "kandev": "Fallback workspace",
        "control_plane": "Fallback control",
        "github": "GitHub",
        "codex": "Codex",
    }
    result: dict[str, BackendHealth] = {}
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
        result[name] = BackendHealth(
            capability=name,
            status=mapped,
            user_message=item.user_message,
            repairable=item.repairable,
            technical_detail=str(item.advanced.get("technical_detail", "")),
        )
    return result


def _resolve_startup_binding(service: MemoryService):
    """Force one runtime-affinity binding, or fail closed on conflicts."""
    active_bindings = list_runtime_affinity_bindings(service.store)
    distinct_bindings = {
        (
            binding.workspace_backend,
            binding.agent_backend,
            binding.profile,
        )
        for binding in active_bindings
    }
    if len(distinct_bindings) > 1:
        raise RuntimeError(
            "STARTUP_RECONCILIATION_REQUIRED: multiple active tasks are bound "
            "to different backend profiles; do not silently choose one"
        )
    return active_bindings[0] if active_bindings else None


def _emit_readiness_marker(readiness: ProfileReadiness) -> None:
    """Print one bounded readiness line for the bootstrap start gate."""
    print(
        f"SUPERVISOR_READY status={readiness.status} profile={readiness.profile} "
        f"workspace_backend={readiness.workspace_backend} "
        f"agent_backend={readiness.agent_backend}",
        file=sys.stderr,
        flush=True,
    )


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


if __name__ == "__main__":
    main()
