from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from codex_supervisor_bridge.integrations.kandev_client import KandevAdapter
from codex_supervisor_bridge.integrations.kandev_coordinator import (
    KandevCoordinator,
    KandevProvisionOptions,
)
from codex_supervisor_bridge.integrations.kandev_errors import KandevToolError
from codex_supervisor_bridge.memory.models import EventType
from codex_supervisor_bridge.memory.service import MemoryService


def build_fake_kandev() -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-kandev")
    state: dict[str, Any] = {"created": {}, "calls": []}

    def dump(payload: dict[str, Any]) -> str:
        return json.dumps(payload)

    @server.tool()
    def list_workspaces_kandev() -> str:
        return dump({"workspaces": [{"id": "ws-1", "name": "Default"}], "total": 1})

    @server.tool()
    def list_workflows_kandev(workspace_id: str) -> str:
        return dump({"workflows": [{"id": "wf-1", "workspace_id": workspace_id}], "total": 1})

    @server.tool()
    def list_workflow_steps_kandev(workflow_id: str) -> str:
        return dump({"workflow_steps": [{"id": "step-1", "workflow_id": workflow_id}], "total": 1})

    @server.tool()
    def list_repositories_kandev(workspace_id: str) -> str:
        return dump({"repositories": [{"id": "repo-1", "workspace_id": workspace_id}], "total": 1})

    @server.tool()
    def list_tasks_kandev(workflow_id: str) -> str:
        return dump({"tasks": list(state["created"].values()), "workflow_id": workflow_id})

    @server.tool()
    def create_task_kandev(
        title: str,
        prompt: str | None = None,
        workspace_id: str | None = None,
        workflow_id: str | None = None,
        workflow_step_id: str | None = None,
        agent_profile_id: str | None = None,
        executor_profile_id: str | None = None,
        start_agent: bool = True,
        repository_id: str | None = None,
        repository_url: str | None = None,
        local_path: str | None = None,
        base_branch: str | None = None,
        external_id: str | None = None,
        parent_id: str | None = None,
        workspace_mode: str | None = None,
        autopilot: bool = False,
        blocked_by: list[str] | None = None,
        start_when_unblocked: bool | None = None,
    ) -> str:
        args = locals().copy()
        state["calls"].append(args)
        key = external_id or f"anonymous-{len(state['created']) + 1}"
        if key not in state["created"]:
            state["created"][key] = {
                "id": f"ktask-{len(state['created']) + 1}",
                "title": title,
                "external_id": external_id,
                "start_agent": start_agent,
                "workflow_id": workflow_id,
                "workspace_id": workspace_id,
            }
        return dump(state["created"][key])

    @server.tool()
    def list_task_sessions_kandev(task_id: str) -> str:
        return dump({"task_id": task_id, "sessions": [{"id": "session-1", "state": "idle"}]})

    @server.tool()
    def get_task_conversation_kandev(
        task_id: str,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> str:
        return dump(
            {
                "task_id": task_id,
                "session_id": session_id,
                "limit": limit,
                "messages": [{"role": "assistant", "text": "hello"}],
            }
        )

    @server.tool()
    def move_task_kandev(
        task_id: str,
        workflow_id: str,
        workflow_step_id: str,
        position: int | None = None,
        prompt: str | None = None,
    ) -> str:
        return dump(
            {
                "id": task_id,
                "workflow_id": workflow_id,
                "workflow_step_id": workflow_step_id,
                "position": position,
                "prompt": prompt,
            }
        )

    @server.tool()
    def update_task_state_kandev(task_id: str, state: str) -> str:
        return dump({"id": task_id, "state": state})

    return server, state


def test_adapter_discovers_required_external_surface_and_parses_json_text() -> None:
    server, _ = build_fake_kandev()

    async def scenario() -> None:
        async with KandevAdapter(server) as adapter:
            capabilities = await adapter.require_compatible()
            assert capabilities.compatible is True

            workspaces = await adapter.list_workspaces()
            assert workspaces["total"] == 1
            assert workspaces["workspaces"][0]["id"] == "ws-1"

            sessions = await adapter.list_task_sessions("ktask-1")
            assert sessions["sessions"][0]["state"] == "idle"

    asyncio.run(scenario())


def test_coordinator_provisions_without_starting_agent_and_binds_revision() -> None:
    server, state = build_fake_kandev()
    memory = MemoryService()
    task = memory.create_task(
        "GAME-401",
        "Persistent task integration",
        repository="cuteyuchen/game",
        goal="Prepare the Kandev task but do not start Codex yet.",
    )
    coordinator = KandevCoordinator(memory, lambda: KandevAdapter(server))

    async def scenario() -> None:
        binding = await coordinator.provision_task(
            task.task_id,
            task.revision,
            options=KandevProvisionOptions(
                workspace_id="ws-1",
                workflow_id="wf-1",
                repository_id="repo-1",
                base_branch="main",
                agent_profile_id="agent-profile-1",
                executor_profile_id="executor-profile-1",
            ),
        )
        assert binding.kandev_task_id == "ktask-1"
        assert binding.external_id == "codex-supervisor-bridge:GAME-401"

        current = memory.get_task(task.task_id)
        assert current.external_kandev_task_id == "ktask-1"
        assert current.revision == task.revision + 1
        assert state["calls"][0]["start_agent"] is False
        assert state["calls"][0]["autopilot"] is False
        assert state["calls"][0]["external_id"] == binding.external_id

        events = memory.timeline(task.task_id)
        assert events[-1].event_type == EventType.KANDEV_TASK_BOUND

        # Replaying provisioning after a successful bind is a local no-op and
        # cannot accidentally create or start a second Kandev task.
        replay = await coordinator.provision_task(task.task_id, current.revision)
        assert replay.kandev_task_id == "ktask-1"
        assert len(state["calls"]) == 1
        assert memory.get_task(task.task_id).revision == current.revision

        sessions = await coordinator.list_linked_sessions(task.task_id)
        assert sessions["linked"] is True
        assert sessions["remote"]["sessions"][0]["id"] == "session-1"

        conversation = await coordinator.get_linked_conversation(task.task_id, limit=20)
        assert conversation["remote"]["messages"][0]["text"] == "hello"

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_adapter_propagates_remote_tool_error() -> None:
    server = MCPServer("failing-kandev")

    @server.tool()
    def list_workspaces_kandev() -> str:
        raise ToolError("backend unavailable")

    async def scenario() -> None:
        async with KandevAdapter(server) as adapter:
            with pytest.raises(KandevToolError, match="backend unavailable"):
                await adapter.list_workspaces()

    asyncio.run(scenario())
