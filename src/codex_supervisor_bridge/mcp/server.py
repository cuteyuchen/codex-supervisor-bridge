from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server import MCPServer

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.config import Settings
from codex_supervisor_bridge.integrations.codex_control_client import CodexControlAdapter
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.integrations.control_plane_agent import ControlPlaneAgentBackend
from codex_supervisor_bridge.integrations.devspace_client import DevSpaceWorkspaceAdapter
from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService
from codex_supervisor_bridge.supervisor.direct_workspace import DirectWorkspaceCoordinator

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
""".strip()


def create_mcp_server(
    service: MemoryService,
    *,
    kandev: KandevCoordinator | None = None,
    codex: CodexCoordinator | None = None,
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
    if codex is not None:
        register_codex_tools(server, codex)
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

    service = MemoryService(args.database)
    kandev = KandevCoordinator(
        service,
        lambda: KandevAdapter(args.kandev_mcp_url),
    )
    direct_workspace = DirectWorkspaceCoordinator(
        service,
        lambda: DevSpaceWorkspaceAdapter(args.devspace_mcp_url),
    )
    codex = CodexCoordinator(
        service,
        lambda: CodexControlAdapter.stdio(
            args.codex_control_command,
            env={"CODEX_MCP_EXECUTION_MODE": "client"},
        ),
    )
    server = create_mcp_server(
        service,
        kandev=kandev,
        codex=codex,
        direct_workspace=direct_workspace,
    )
    try:
        if args.transport == "stdio":
            server.run(transport="stdio")
        else:
            server.run(
                transport="streamable-http",
                host=args.host,
                port=args.port,
                streamable_http_path=args.mcp_path,
                json_response=True,
                stateless_http=True,
            )
    finally:
        service.close()


if __name__ == "__main__":
    main()
