from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client

from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def structured(result: Any) -> dict[str, Any]:
    value = result.structured_content
    assert isinstance(value, dict)
    return value


def test_list_supervised_tasks_discovers_recent_active_tasks_without_native_ids() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            first = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "DISCOVERY-A",
                    "title": "First task",
                    "repository": r"C:\repo-a",
                    "goal": "First goal",
                },
            )
            assert first.is_error is False

            second = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "DISCOVERY-B",
                    "title": "Second task",
                    "repository": r"C:\repo-b",
                    "goal": "Second goal",
                },
            )
            assert second.is_error is False
            second_data = structured(second)["task"]

            bumped = await client.call_tool(
                "record_user_override",
                {
                    "task_id": "DISCOVERY-A",
                    "expected_revision": 0,
                    "instruction": "Make this the most recently touched task.",
                },
            )
            assert bumped.is_error is False

            listed = await client.call_tool(
                "list_supervised_tasks",
                {"active_only": True, "limit": 10},
            )
            assert listed.is_error is False
            tasks = structured(listed)["tasks"]
            assert [item["task_id"] for item in tasks] == ["DISCOVERY-A", "DISCOVERY-B"]
            assert tasks[0]["revision"] == 1
            assert tasks[1]["revision"] == second_data["revision"]
            assert "codex_thread_id" not in tasks[0]
            assert "codex_turn_id" not in tasks[0]
            assert "git_head" not in tasks[0]

            filtered = await client.call_tool(
                "list_supervised_tasks",
                {"repository": r"C:\repo-b", "limit": 10},
            )
            assert filtered.is_error is False
            assert [item["task_id"] for item in structured(filtered)["tasks"]] == [
                "DISCOVERY-B"
            ]

            invalid = await client.call_tool(
                "list_supervised_tasks",
                {"limit": 101},
            )
            assert invalid.is_error is True

            # Discovery is read-only: it must not advance task revisions.
            after = await client.call_tool(
                "get_supervised_task",
                {"task_id": "DISCOVERY-A"},
            )
            assert structured(after)["task"]["revision"] == 1

    try:
        run(scenario())
    finally:
        service.close()
