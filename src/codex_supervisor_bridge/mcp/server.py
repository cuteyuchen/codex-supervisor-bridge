from __future__ import annotations

import argparse
from pathlib import Path

from mcp.server import MCPServer

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.config import Settings
from codex_supervisor_bridge.memory.service import MemoryService

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

This server intentionally exposes semantic supervisor operations only. It does
not provide arbitrary shell, process execution, raw SQL, or unrestricted
filesystem tools.
""".strip()


def create_mcp_server(service: MemoryService) -> MCPServer:
    server = MCPServer(
        "codex-supervisor-bridge",
        title="Codex Supervisor Bridge",
        description="Persistent memory and supervision bridge for ChatGPT and Codex.",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    register_memory_tools(server, service)
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
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not args.mcp_path.startswith("/"):
        parser.error("--mcp-path must start with '/'")

    service = MemoryService(args.database)
    server = create_mcp_server(service)
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
