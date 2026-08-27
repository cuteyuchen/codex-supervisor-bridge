from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client

from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def structured(result: Any) -> dict[str, Any]:
    value = result.structured_content
    assert isinstance(value, dict)
    return value


def test_execution_tools_are_semantic_bounded_and_annotated() -> None:
    memory = MemoryService()
    server = create_mcp_server(memory)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            expected = {
                "get_task_execution_state",
                "get_execution_handoffs",
                "set_task_execution_mode",
                "set_codex_delegation_policy",
                "acquire_chatgpt_workspace_writer",
                "acquire_codex_workspace_writer",
                "release_workspace_writer",
                "handoff_workspace_writer",
            }
            assert expected <= set(tools)

            # Direct workspace mutation does not exist until the supervised
            # WorkspaceBackend facade is installed; raw shell/backend tools stay hidden.
            for forbidden in {
                "bash",
                "exec_command",
                "apply_patch",
                "write",
                "open_workspace",
                "codex_submit_task",
            }:
                assert forbidden not in tools

            for name in {"get_task_execution_state", "get_execution_handoffs"}:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.open_world_hint is False

            for name in expected - {"get_task_execution_state", "get_execution_handoffs"}:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_hybrid_writer_handoff_through_mcp_is_revision_and_epoch_fenced() -> None:
    memory = MemoryService()
    server = create_mcp_server(memory)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {"task_id": "EXEC-MCP", "title": "Execution MCP"},
            )
            revision = structured(created)["task"]["revision"]

            state = await client.call_tool(
                "get_task_execution_state",
                {"task_id": "EXEC-MCP"},
            )
            state_data = structured(state)
            assert state_data["execution_mode"] == "HYBRID"
            assert state_data["active_writer"] == "NONE"
            assert state_data["handoff_policy"] == "MANUAL_ONLY"

            acquired = await client.call_tool(
                "acquire_chatgpt_workspace_writer",
                {"task_id": "EXEC-MCP", "expected_revision": revision},
            )
            acquired_data = structured(acquired)
            revision = acquired_data["task"]["revision"]
            epoch = acquired_data["execution"]["writer_epoch"]
            assert acquired_data["execution"]["active_writer"] == "CHATGPT"
            assert epoch == 1

            blocked = await client.call_tool(
                "handoff_workspace_writer",
                {
                    "task_id": "EXEC-MCP",
                    "expected_revision": revision,
                    "from_writer": "CHATGPT",
                    "to_writer": "CODEX",
                    "expected_writer_epoch": epoch,
                    "reason": "Supervisor wants Codex without user permission",
                },
            )
            assert blocked.is_error is True

            # A one-time explicit user authorization allows this one handoff
            # without changing the durable MANUAL_ONLY policy.
            handed = await client.call_tool(
                "handoff_workspace_writer",
                {
                    "task_id": "EXEC-MCP",
                    "expected_revision": revision,
                    "from_writer": "CHATGPT",
                    "to_writer": "CODEX",
                    "expected_writer_epoch": epoch,
                    "reason": "User explicitly delegated this bounded implementation",
                    "git_head": "abc123",
                    "change_ref": "review:direct-1",
                    "validation": {"pytest": "passed"},
                    "user_explicitly_authorized_codex": True,
                },
            )
            handed_data = structured(handed)
            assert handed_data["execution"]["active_writer"] == "CODEX"
            assert handed_data["execution"]["writer_epoch"] == 2
            assert handed_data["handoff"]["change_ref"] == "review:direct-1"

            stale_release = await client.call_tool(
                "release_workspace_writer",
                {
                    "task_id": "EXEC-MCP",
                    "expected_revision": handed_data["task"]["revision"],
                    "writer": "CODEX",
                    "expected_writer_epoch": 1,
                },
            )
            assert stale_release.is_error is True

            context = await client.call_tool(
                "get_context_pack",
                {"task_id": "EXEC-MCP"},
            )
            text = structured(context)["content"]
            assert "EXECUTION STATE" in text
            assert "Execution Mode: HYBRID" in text
            assert "Active Writer: CODEX" in text
            assert "Writer Epoch: 2" in text

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
