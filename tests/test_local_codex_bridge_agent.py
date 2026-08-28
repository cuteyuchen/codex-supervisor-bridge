from __future__ import annotations

import asyncio
from typing import Any

from mcp.server import MCPServer

from codex_supervisor_bridge.backends.models import (
    BackendHealthStatus,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.integrations.local_codex_bridge_client import (
    LocalCodexBridgeAgentBackend,
)
from codex_supervisor_bridge.memory.models import ActiveWriter


def fake_bridge() -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-local-codex-bridge")
    state: dict[str, Any] = {"calls": [], "observe_payload": None}

    @server.tool()
    def codex_turn(
        text: str,
        thread_id: str | None = None,
        cwd: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
    ) -> dict[str, Any]:
        state["calls"].append(("turn", text, thread_id, cwd, sandbox, approval_policy))
        return {
            "accepted": True,
            "thread_id": thread_id or "thread-1",
            "turn_id": "turn-1",
            "status": "inProgress",
        }

    @server.tool()
    def codex_observe(
        thread_id: str,
        cursor: int | None = None,
        limit: int | None = None,
        wait_ms: int | None = None,
    ) -> dict[str, Any]:
        state["calls"].append(("observe", thread_id, cursor, limit, wait_ms))
        if state["observe_payload"] is not None:
            return state["observe_payload"]
        return {
            "runtime_status": "inProgress",
            "thread_id": thread_id,
            "turn_id": "turn-1",
            "events": [{"method": "item/status/updated", "summary": "Implementing"}],
            "pending_requests": [
                {
                    "id": 17,
                    "method": "item/commandExecution/requestApproval",
                    "params": {"command": "pytest", "prompt": "Run tests?"},
                }
            ],
            "files_changed": ["src/app.py"],
        }

    @server.tool()
    def codex_steer(thread_id: str, expected_turn_id: str, text: str) -> dict[str, Any]:
        state["calls"].append(("steer", thread_id, expected_turn_id, text))
        return {"status": "inProgress", "thread_id": thread_id, "turn_id": expected_turn_id}

    @server.tool()
    def codex_respond(
        request_id: str | int,
        thread_id: str,
        method: str,
        turn_id: str | None = None,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str | None = None,
    ) -> dict[str, Any]:
        state["calls"].append(
            ("respond", request_id, thread_id, method, turn_id, decision, answers, scope)
        )
        return {"status": "inProgress", "thread_id": thread_id, "turn_id": turn_id}

    @server.tool()
    def codex_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
        state["calls"].append(("interrupt", thread_id, turn_id))
        return {"status": "interrupted", "thread_id": thread_id, "turn_id": turn_id}

    return server, state


def lease() -> WriterLeaseToken:
    return WriterLeaseToken(
        task_id="TASK-AGENT",
        writer=ActiveWriter.CODEX,
        writer_epoch=2,
        task_revision=4,
    )


def test_local_codex_bridge_normalizes_live_control_surface() -> None:
    upstream, state = fake_bridge()
    backend = LocalCodexBridgeAgentBackend(upstream)

    async def scenario() -> None:
        health = await backend.health()
        assert health.status == BackendHealthStatus.READY
        handle = await backend.start_plan(
            task_id="TASK-AGENT",
            context_pack="Goal: test",
            workspace=WorkspaceState(workspace_id="ws", repository="repo", root="C:/repo"),
        )
        assert handle.thread_id == "thread-1"
        assert handle.turn_id == "turn-1"
        assert handle.status == "planning"

        snapshot = await backend.observe(handle, cursor=3, wait_ms=50)
        assert snapshot.status == "inProgress"
        assert snapshot.files_changed == ["src/app.py"]
        assert len(snapshot.pending_interactions) == 1
        interaction = snapshot.pending_interactions[0]
        assert interaction.kind == "command_approval"
        assert interaction.interaction_id == "17"
        assert interaction.runtime_reference["method"] == "item/commandExecution/requestApproval"

        steered = await backend.steer(handle, "Run the focused test first.", lease=lease())
        assert steered.turn_id == "turn-1"
        answered = await backend.respond_interaction(
            handle,
            interaction,
            {"decision": "accept", "scope": "turn"},
        )
        assert answered.status == "inProgress"
        interrupted = await backend.interrupt(handle)
        assert interrupted.status == "interrupted"

    asyncio.run(scenario())
    assert any(call[0] == "steer" and call[2] == "turn-1" for call in state["calls"])
    assert any(call[0] == "respond" and call[1] == 17 for call in state["calls"])


def test_completed_plan_uses_terminal_final_result_without_reclassifying_execution() -> None:
    upstream, state = fake_bridge()
    backend = LocalCodexBridgeAgentBackend(upstream)
    state["observe_payload"] = {
        "runtime_status": "completed",
        "thread_id": "thread-1",
        "turn_id": "turn-1",
        "terminal": {
            "status": "completed",
            "final_result": "1. Add the smoke marker.\n2. Verify the diff.",
        },
    }

    async def scenario() -> None:
        plan_handle = await backend.start_plan(
            task_id="TASK-AGENT",
            context_pack="Goal: test",
            workspace=WorkspaceState(workspace_id="ws", repository="repo", root="C:/repo"),
        )
        assert plan_handle.status == "planning"

        plan_snapshot = await backend.observe(plan_handle)
        assert plan_snapshot.status == "completed"
        assert plan_snapshot.plan is not None
        assert plan_snapshot.plan.content == "1. Add the smoke marker.\n2. Verify the diff."

        execution_handle = await backend.start_execution(
            task_id="TASK-AGENT",
            context_pack="Goal: test",
            approved_plan=plan_snapshot.plan.content,
            workspace=WorkspaceState(workspace_id="ws", repository="repo", root="C:/repo"),
            lease=lease(),
        )
        assert execution_handle.status == "inProgress"

        execution_snapshot = await backend.observe(execution_handle)
        assert execution_snapshot.status == "completed"
        assert execution_snapshot.plan is None

    asyncio.run(scenario())


def test_unknown_outcome_handle_is_explicit_and_not_failed() -> None:
    from codex_supervisor_bridge.integrations.agent_backends import _unknown_handle

    handle = _unknown_handle("codex_turn")
    assert handle.status == "UNKNOWN"
    assert handle.reconciliation_required is True
    assert "retry" in (handle.message or "")
