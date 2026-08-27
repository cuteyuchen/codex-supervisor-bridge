from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server import MCPServer

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.config import Settings
from codex_supervisor_bridge.integrations.codex_control_client import CodexControlAdapter
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.checkpoints import CheckpointService

from .checkpoint_tools import register_checkpoint_tools
from .codex_tools import register_codex_tools
from .kandev_tools import register_kandev_tools
from .tools import register_memory_tools

SERVER_INSTRUCTIONS = """
You are connected to Codex Supervisor Bridge, the durable supervisory control
surface for development tasks. Chat history is not the source of truth.

Before making a task mutation, read the current task/context and use its latest
revision as expected_revision. If a tool reports STALE_CONTEXT, do not retry
with the old revision: re-read canonical task/context state first.

ACTIVE decisions and constraints are current instructions. Search may also
return SUPERSEDED historical records; never treat a superseded record as
current truth. HARD constraints outrank plans and agent-local choices.

Kandev owns workflow/worktree facts. Codex Control Plane owns Codex runtime
facts. Supervisor Bridge owns user intent, active constraints, revision locks,
plan approval, checkpoint review, and cross-system routing.

Codex implementation is plan-gated: start_codex_plan is read-only Plan Mode;
import_codex_plan creates a local DRAFT; approve_task_plan is the explicit
Supervisor gate; execute_codex_approved_plan re-checks that the remote
latestPlan still matches the locally approved plan before workspace-write is
allowed.

Use collect_codex_checkpoint to compress current Codex progress into a bounded
HEARTBEAT, PROGRESS, or GATE checkpoint. PROGRESS and GATE checkpoints require
review. Use review_codex_checkpoint to record CONTINUE, STEER, INTERRUPT,
REPLAN, or ACCEPT. Follow-up control remains explicit: a STEER review should
be followed by soft_steer_codex; INTERRUPT/REPLAN should be followed by
interrupt_codex. P6 will make hard replan atomic.

Use soft_steer_codex only for a local correction while the current plan remains
valid. For architecture/scope changes, interrupt first and create/review a new
plan. Raw codex_submit_task and danger-full-access are intentionally not
exposed through this server.

This server intentionally exposes semantic supervisor operations only. It does
not provide arbitrary shell, process execution, raw SQL, or unrestricted
filesystem tools.
""".strip()


def create_mcp_server(
    service: MemoryService,
    *,
    kandev: KandevCoordinator | None = None,
    codex: CodexCoordinator | None = None,
) -> MCPServer:
    server = MCPServer(
        "codex-supervisor-bridge",
        title="Codex Supervisor Bridge",
        description="Persistent memory and supervision bridge for ChatGPT and Codex.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    register_memory_tools(server, service)
    if kandev is not None:
        register_kandev_tools(server, kandev)
    if codex is not None:
        register_codex_tools(server, codex)
        register_checkpoint_tools(server, service, CheckpointService(service, codex))
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
        help="Kandev external MCP endpoint (default: http://127.0.0.1:38429/mcp)",
    )
    parser.add_argument(
        "--codex-control-command",
        default=settings.codex_control_command,
        help="Codex Control Plane MCP executable (default: codex-control-plane-mcp)",
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
    if not args.codex_control_command.strip():
        parser.error("--codex-control-command must not be empty")

    service = MemoryService(args.database)
    kandev = KandevCoordinator(
        service,
        lambda: KandevAdapter(args.kandev_mcp_url),
    )
    codex = CodexCoordinator(
        service,
        lambda: CodexControlAdapter.stdio(
            args.codex_control_command,
            env={"CODEX_MCP_EXECUTION_MODE": "client"},
        ),
    )
    server = create_mcp_server(service, kandev=kandev, codex=codex)
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
