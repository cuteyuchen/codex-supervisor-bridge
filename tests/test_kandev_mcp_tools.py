from __future__ import annotations

import asyncio
from typing import Any

from mcp import Client
from mcp.server import MCPServer

from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
from codex_supervisor_bridge.integrations.kandev_coordinator import KandevCoordinator
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def structured(result: Any) -> dict[str, Any]:
    value = result.structured_content
    assert isinstance(value, dict)
    return value


def build_structured_fake_kandev() -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-kandev-structured")
    state: dict[str, Any] = {"create_calls": []}

    @server.tool()
    def list_workspaces_kandev() -> dict[str, Any]:
        return {"workspaces": [{"id": "ws-1"}], "total": 1}

    @server.tool()
    def list_workflows_kandev(workspace_id: str) -> dict[str, Any]:
        return {"workflows": [{"id": "wf-1", "workspace_id": workspace_id}], "total": 1}

    @server.tool()
    def list_workflow_steps_kandev(workflow_id: str) -> dict[str, Any]:
        return {"workflow_steps": [{"id": "step-1", "workflow_id": workflow_id}], "total": 1}

    @server.tool()
    def list_repositories_kandev(workspace_id: str) -> dict[str, Any]:
        return {"repositories": [{"id": "repo-1", "workspace_id": workspace_id}], "total": 1}

    @server.tool()
    def list_tasks_kandev(workflow_id: str) -> dict[str, Any]:
        return {"tasks": [], "workflow_id": workflow_id}

    @server.tool()
    def create_task_kandev(
        title: str,
        prompt: str | None = None,
        workspace_id: str | None = None,
        workflow_id: str | None = None,
        workflow_step_id: str | None = None,
        workspace_mode: str | None = None,
        autopilot: bool = False,
        agent_profile_id: str | None = None,
        executor_profile_id: str | None = None,
        start_agent: bool = True,
        repository_id: str | None = None,
        local_path: str | None = None,
        repository_url: str | None = None,
        base_branch: str | None = None,
        external_id: str | None = None,
        parent_id: str | None = None,
        blocked_by: list[str] | None = None,
        start_when_unblocked: bool | None = None,
    ) -> dict[str, Any]:
        call = locals().copy()
        state["create_calls"].append(call)
        return {
            "id": "ktask-1",
            "title": title,
            "external_id": external_id,
            "start_agent": start_agent,
            "autopilot": autopilot,
        }

    @server.tool()
    def list_task_sessions_kandev(task_id: str) -> dict[str, Any]:
        return {"task_id": task_id, "sessions": [{"id": "session-1", "state": "idle"}]}

    @server.tool()
    def get_task_conversation_kandev(
        task_id: str,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        return {
            "task_id": task_id,
            "session_id": session_id,
            "limit": limit,
            "messages": [{"role": "assistant", "text": "prepared"}],
        }

    @server.tool()
    def move_task_kandev(
        task_id: str,
        workflow_id: str,
        workflow_step_id: str,
        position: int | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": task_id,
            "workflow_id": workflow_id,
            "workflow_step_id": workflow_step_id,
            "position": position,
            "prompt": prompt,
        }

    @server.tool()
    def update_task_state_kandev(task_id: str, state: str) -> dict[str, Any]:
        return {"id": task_id, "state": state}

    return server, state


def test_supervisor_mcp_exposes_four_safe_kandev_tools() -> None:
    kandev_server, _ = build_structured_fake_kandev()
    memory = MemoryService()
    coordinator = KandevCoordinator(memory, lambda: KandevAdapter(kandev_server))
    supervisor = create_mcp_server(memory, kandev=coordinator)

    async def scenario() -> None:
        async with Client(supervisor) as client:
            listed = await client.list_tools()
            names = {tool.name for tool in listed.tools}
            assert {
                "get_kandev_capabilities",
                "provision_kandev_task",
                "get_kandev_sessions",
                "get_kandev_conversation",
            } <= names

            # Supervisor exposes only bounded Kandev semantics. Raw upstream
            # creation/movement/state tools stay hidden even as unrelated
            # Supervisor capabilities are added later.
            for forbidden in {
                "list_workspaces_kandev",
                "list_workflows_kandev",
                "list_workflow_steps_kandev",
                "list_repositories_kandev",
                "list_tasks_kandev",
                "create_task_kandev",
                "list_task_sessions_kandev",
                "get_task_conversation_kandev",
                "move_task_kandev",
                "update_task_state_kandev",
            }:
                assert forbidden not in names

            tools = {tool.name: tool for tool in listed.tools}
            assert tools["get_kandev_capabilities"].annotations is not None
            assert tools["get_kandev_capabilities"].annotations.read_only_hint is True
            assert tools["provision_kandev_task"].annotations is not None
            assert tools["provision_kandev_task"].annotations.read_only_hint is False
            assert tools["provision_kandev_task"].annotations.destructive_hint is False

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_full_supervisor_to_kandev_provision_and_observation_flow() -> None:
    kandev_server, state = build_structured_fake_kandev()
    memory = MemoryService()
    coordinator = KandevCoordinator(memory, lambda: KandevAdapter(kandev_server))
    supervisor = create_mcp_server(memory, kandev=coordinator)

    async def scenario() -> None:
        async with Client(supervisor) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "GAME-501",
                    "title": "Kandev integration",
                    "repository": "cuteyuchen/game",
                    "goal": "Prepare a supervised Kandev task.",
                },
            )
            created_data = structured(created)
            revision = created_data["task"]["revision"]

            capabilities = await client.call_tool("get_kandev_capabilities", {})
            capabilities_data = structured(capabilities)
            assert capabilities_data["compatible"] is True
            assert capabilities_data["missing_required_tools"] == []

            provisioned = await client.call_tool(
                "provision_kandev_task",
                {
                    "task_id": "GAME-501",
                    "expected_revision": revision,
                    "workspace_id": "ws-1",
                    "workflow_id": "wf-1",
                    "workflow_step_id": "step-1",
                    "repository_id": "repo-1",
                    "base_branch": "main",
                },
            )
            assert provisioned.is_error is False
            provisioned_data = structured(provisioned)
            assert provisioned_data["binding"]["kandev_task_id"] == "ktask-1"
            assert provisioned_data["task"]["external_kandev_task_id"] == "ktask-1"
            assert provisioned_data["task"]["revision"] == revision + 1

            call = state["create_calls"][0]
            assert call["start_agent"] is False
            assert call["autopilot"] is False
            assert call["external_id"] == "codex-supervisor-bridge:GAME-501"

            sessions = await client.call_tool(
                "get_kandev_sessions",
                {"task_id": "GAME-501"},
            )
            sessions_data = structured(sessions)
            assert sessions_data["linked"] is True
            assert sessions_data["remote"]["sessions"][0]["id"] == "session-1"

            conversation = await client.call_tool(
                "get_kandev_conversation",
                {"task_id": "GAME-501", "limit": 20},
            )
            conversation_data = structured(conversation)
            assert conversation_data["remote"]["messages"][0]["text"] == "prepared"

            # Replaying with the latest revision uses the local binding and does
            # not ask Kandev to create/start another task.
            replay = await client.call_tool(
                "provision_kandev_task",
                {
                    "task_id": "GAME-501",
                    "expected_revision": provisioned_data["task"]["revision"],
                },
            )
            assert replay.is_error is False
            assert len(state["create_calls"]) == 1
            assert structured(replay)["task"]["revision"] == provisioned_data["task"]["revision"]

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
