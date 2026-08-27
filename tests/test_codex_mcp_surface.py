from __future__ import annotations

import asyncio
import json

from mcp import Client
from mcp.server import MCPServer

from codex_supervisor_bridge.integrations.codex_control_client import (
    REQUIRED_STABLE_TOOLS,
    CodexControlAdapter,
)
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def test_control_adapter_accepts_wrapped_json_string_result() -> None:
    upstream = MCPServer("wrapped-control-plane")

    @upstream.tool()
    def codex_health_summary() -> str:
        return json.dumps(
            {
                "ok": True,
                "version": {
                    "serverName": "codex-control-plane-mcp",
                    "contractVersion": "1",
                    "toolSurfaceHash": "surface",
                    "guideHash": "guide",
                    "stableTools": sorted(REQUIRED_STABLE_TOOLS),
                },
            }
        )

    async def scenario() -> None:
        async with CodexControlAdapter(upstream) as adapter:
            health = await adapter.health()
            assert health["ok"] is True
            assert health["version"]["contractVersion"] == "1"

    asyncio.run(scenario())


def test_chatgpt_surface_exposes_guarded_codex_and_checkpoint_tools_only() -> None:
    upstream = MCPServer("unused-control-plane")
    memory = MemoryService()
    coordinator = CodexCoordinator(memory, lambda: CodexControlAdapter(upstream))
    server = create_mcp_server(memory, codex=coordinator)

    async def scenario() -> None:
        async with Client(server) as client:
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}

            expected_codex = {
                "get_codex_control_health",
                "get_codex_runtime_capabilities",
                "preflight_codex_project",
                "start_codex_plan",
                "get_codex_status",
                "import_codex_plan",
                "execute_codex_approved_plan",
                "soft_steer_codex",
                "interrupt_codex",
                "list_codex_pending_interactions",
                "answer_codex_pending_interaction",
            }
            expected_checkpoints = {
                "collect_codex_checkpoint",
                "get_latest_codex_checkpoint",
                "list_codex_checkpoints",
                "review_codex_checkpoint",
            }
            assert expected_codex | expected_checkpoints <= set(tools)

            assert "codex_submit_task" not in tools
            assert "codex_approve_plan" not in tools
            assert "codex_interrupt_turn" not in tools
            assert "codex_get_operation_status" not in tools

            execute_schema = tools["execute_codex_approved_plan"].input_schema
            assert "sandbox" not in execute_schema.get("properties", {})
            start_schema = tools["start_codex_plan"].input_schema
            assert "sandbox" not in start_schema.get("properties", {})

            read_only = {
                "get_codex_control_health",
                "get_codex_runtime_capabilities",
                "preflight_codex_project",
                "get_codex_status",
                "list_codex_pending_interactions",
                "get_latest_codex_checkpoint",
                "list_codex_checkpoints",
            }
            for name in read_only:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is True
                assert annotations.open_world_hint is False

            mutating = (expected_codex | expected_checkpoints) - read_only
            for name in mutating:
                annotations = tools[name].annotations
                assert annotations is not None
                assert annotations.read_only_hint is False
                assert annotations.destructive_hint is False
                assert annotations.open_world_hint is False

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
