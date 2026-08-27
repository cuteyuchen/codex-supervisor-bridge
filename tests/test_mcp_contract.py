from __future__ import annotations

import asyncio

from mcp import Client

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def test_server_identity_and_tool_annotations() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            assert client.server_info is not None
            assert client.server_info.name == "codex-supervisor-bridge"
            assert client.server_info.version == __version__

            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}

            expected = {
                "create_supervised_task",
                "get_supervised_task",
                "resume_supervised_task",
                "get_context_pack",
                "search_task_memory",
                "get_task_timeline",
                "record_user_override",
                "update_task_intent",
                "add_task_decision",
                "supersede_task_decision",
                "add_task_constraint",
                "supersede_task_constraint",
                "create_task_plan",
                "get_current_plan",
                "approve_task_plan",
                "reject_task_plan",
            }
            assert set(tools) == expected

            for name in {
                "get_supervised_task",
                "resume_supervised_task",
                "get_context_pack",
                "search_task_memory",
                "get_task_timeline",
                "get_current_plan",
            }:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.open_world_hint is False

            for name in expected - {
                "get_supervised_task",
                "resume_supervised_task",
                "get_context_pack",
                "search_task_memory",
                "get_task_timeline",
                "get_current_plan",
            }:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False

    try:
        asyncio.run(scenario())
    finally:
        service.close()
