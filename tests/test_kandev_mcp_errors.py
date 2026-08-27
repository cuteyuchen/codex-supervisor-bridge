from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client

from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.integrations.kandev_errors import KandevError
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def text_content(result: Any) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "text", None) is not None
    )


class UnavailableAdapter:
    async def __aenter__(self) -> "UnavailableAdapter":
        raise KandevError("Kandev external MCP is unavailable")

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        return None


def test_multiple_repository_locators_are_model_readable_tool_error() -> None:
    memory = MemoryService()
    coordinator = KandevCoordinator(memory, lambda: UnavailableAdapter())
    supervisor = create_mcp_server(memory, kandev=coordinator)

    async def scenario() -> None:
        async with Client(supervisor) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {"task_id": "TASK-LOCATOR", "title": "Locator validation"},
            )
            revision = created.structured_content["task"]["revision"]

            result = await client.call_tool(
                "provision_kandev_task",
                {
                    "task_id": "TASK-LOCATOR",
                    "expected_revision": revision,
                    "repository_id": "repo-1",
                    "repository_url": "https://example.invalid/repo.git",
                },
            )
            assert result.is_error is True
            assert "Pass at most one of repository_id" in text_content(result)
            assert memory.get_task("TASK-LOCATOR").revision == revision
            assert memory.get_task("TASK-LOCATOR").external_kandev_task_id is None

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_unavailable_kandev_is_safe_model_readable_error() -> None:
    memory = MemoryService()
    coordinator = KandevCoordinator(memory, lambda: UnavailableAdapter())
    supervisor = create_mcp_server(memory, kandev=coordinator)

    async def scenario() -> None:
        async with Client(supervisor) as client:
            result = await client.call_tool("get_kandev_capabilities", {})
            assert result.is_error is True
            text = text_content(result)
            assert "Kandev external MCP is unavailable" in text
            assert "Traceback" not in text
            assert "ConnectionRefusedError" not in text

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
