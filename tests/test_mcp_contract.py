from __future__ import annotations

import asyncio

from mcp import Client

from codex_supervisor_bridge import __version__
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


BASE_MEMORY_TOOLS = {
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

EXECUTION_TOOLS = {
    "get_task_execution_state",
    "get_execution_handoffs",
    "set_task_execution_mode",
    "set_codex_delegation_policy",
    "acquire_chatgpt_workspace_writer",
    "acquire_codex_workspace_writer",
    "release_workspace_writer",
    "handoff_workspace_writer",
}

READ_ONLY_TOOLS = {
    "get_supervised_task",
    "resume_supervised_task",
    "get_context_pack",
    "search_task_memory",
    "get_task_timeline",
    "get_current_plan",
    "get_task_execution_state",
    "get_execution_handoffs",
}


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
            expected = BASE_MEMORY_TOOLS | EXECUTION_TOOLS
            assert expected <= set(tools)

            # Adding semantic Supervisor tools is allowed as the product grows,
            # but bypass surfaces remain forbidden on this core-only server.
            for forbidden in {
                "bash",
                "exec_command",
                "apply_patch",
                "write",
                "open_workspace",
                "codex_submit_task",
                "codex_interrupt_turn",
                "create_task_kandev",
                "move_task_kandev",
                "update_task_state_kandev",
            }:
                assert forbidden not in tools

            for name in READ_ONLY_TOOLS:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.open_world_hint is False

            for name in expected - READ_ONLY_TOOLS:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False

    try:
        asyncio.run(scenario())
    finally:
        service.close()
