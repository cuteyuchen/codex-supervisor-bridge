from __future__ import annotations

import asyncio
from typing import Any

import pytest
from mcp.server import MCPServer

from codex_supervisor_bridge.integrations.codex_control_client import (
    REQUIRED_STABLE_TOOLS,
    CodexControlAdapter,
)
from codex_supervisor_bridge.integrations.codex_control_errors import (
    CodexContractError,
    CodexPlanGateError,
)
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.models import EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService


def build_fake_codex() -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-codex-control-plane")
    state: dict[str, Any] = {
        "calls": [],
        "plan_markdown": "# Plan\n\n1. Reuse StorageManager.\n2. Add migration tests.",
        "executing": False,
        "interrupted": False,
    }

    def record(tool: str, args: dict[str, Any]) -> None:
        state["calls"].append((tool, args))

    @server.tool()
    def codex_health_summary() -> dict[str, Any]:
        return {
            "ok": True,
            "version": {
                "serverName": "codex-control-plane-mcp",
                "contractVersion": "1",
                "toolSurfaceHash": "surface-hash",
                "guideHash": "guide-hash",
                "stableTools": sorted(REQUIRED_STABLE_TOOLS),
            },
        }

    @server.tool()
    def codex_get_runtime_capabilities() -> dict[str, Any]:
        return {"ok": True, "status": "ok", "sandbox": {"workspaceWrite": True}}

    @server.tool()
    def codex_preflight_project_run(
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
        live_probe: bool = False,
    ) -> dict[str, Any]:
        args = locals().copy()
        record("preflight", args)
        return {"ok": True, "status": "ready", "checks": [], "liveProbe": live_probe}

    @server.tool()
    def codex_start_plan_workflow(
        project_id: str,
        message: str,
        title: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        sandbox: str | None = "read-only",
        approval_policy: str | None = "on-request",
        client_request_id: str | None = None,
        goal: str | None = None,
    ) -> dict[str, Any]:
        args = locals().copy()
        record("start_plan", args)
        return {
            "ok": True,
            "workflowId": "cwf-1",
            "threadId": "thread-1",
            "planTurnId": "turn-plan-1",
            "planOperationId": "op-plan-1",
            "status": "planning",
            "pollRecommended": True,
            "nextRecommendedAction": "wait_plan",
        }

    @server.tool()
    def codex_get_workflow_status(
        workflow_id: str,
        last_messages: int = 10,
        include_events: bool = True,
        refresh_live: bool = False,
        refresh_live_goal: bool = False,
    ) -> dict[str, Any]:
        record("workflow_status", locals().copy())
        payload: dict[str, Any] = {
            "ok": True,
            "workflowId": workflow_id,
            "threadId": "thread-1",
            "planTurnId": "turn-plan-1",
            "latestPlan": {
                "planQuality": "valid_plan",
                "markdown": state["plan_markdown"],
                "planHash": "hash-1",
                "turnId": "turn-plan-1",
            },
        }
        if state["executing"]:
            payload.update(
                {
                    "status": "executing",
                    "phase": "executing",
                    "executionOperationId": "op-exec-1",
                    "executionTurnId": "turn-exec-1",
                    "pollRecommended": True,
                    "nextRecommendedAction": "wait_execution",
                }
            )
        else:
            payload.update(
                {
                    "status": "plan_ready",
                    "phase": "plan_ready",
                    "pollRecommended": False,
                    "nextRecommendedAction": "review_plan",
                }
            )
        return payload

    @server.tool()
    def codex_approve_plan(
        workflow_id: str,
        client_request_id: str | None = None,
        message: str | None = "Implement the plan.",
        approval_policy: str = "on-request",
        sandbox: str = "read-only",
    ) -> dict[str, Any]:
        args = locals().copy()
        record("approve_plan", args)
        state["executing"] = True
        return {
            "ok": True,
            "workflowId": workflow_id,
            "threadId": "thread-1",
            "executionOperationId": "op-exec-1",
            "executionTurnId": "turn-exec-1",
            "status": "executing",
            "nextRecommendedAction": "wait_execution",
        }

    @server.tool()
    def codex_submit_task(
        operation_type: str,
        client_request_id: str | None = None,
        thread_id: str | None = None,
        expected_turn_id: str | None = None,
        message: str | None = None,
    ) -> dict[str, Any]:
        args = locals().copy()
        record("submit_task", args)
        assert operation_type == "steer_turn"
        return {
            "ok": True,
            "operationId": "op-steer-1",
            "threadId": thread_id,
            "turnId": expected_turn_id,
            "status": "running",
            "pollRecommended": True,
            "nextRecommendedAction": "wait_execution",
        }

    @server.tool()
    def codex_get_operation_status(
        operation_id: str,
        last_messages: int = 10,
        progress_events: int = 10,
        include_events: bool = False,
    ) -> dict[str, Any]:
        record("operation_status", locals().copy())
        return {
            "ok": True,
            "operationId": operation_id,
            "threadId": "thread-1",
            "turnId": "turn-exec-1",
            "status": "running",
            "pollRecommended": True,
            "nextRecommendedAction": "wait_execution",
            "progressEvents": [],
        }

    @server.tool()
    def codex_list_pending_interactions(
        workflow_id: str | None = None,
        operation_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
        status: str | None = "pending",
        limit: int = 50,
    ) -> dict[str, Any]:
        record("list_interactions", locals().copy())
        return {
            "ok": True,
            "interactions": [
                {"interactionId": "interaction-1", "kind": "approval", "status": "pending"}
            ],
        }

    @server.tool()
    def codex_answer_pending_interaction(
        interaction_id: str,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        record("answer_interaction", locals().copy())
        return {"ok": True, "interactionId": interaction_id, "status": "answered"}

    @server.tool()
    def codex_interrupt_turn(
        thread_id: str | None = None,
        turn_id: str | None = None,
        operation_id: str | None = None,
        workflow_id: str | None = None,
    ) -> dict[str, Any]:
        record("interrupt", locals().copy())
        state["interrupted"] = True
        return {
            "ok": True,
            "threadId": thread_id,
            "turnId": turn_id,
            "status": "interrupted",
            "nextRecommendedAction": "none",
        }

    return server, state


def test_contract_handshake_rejects_wrong_server() -> None:
    server = MCPServer("bad-codex")

    @server.tool()
    def codex_health_summary() -> dict[str, Any]:
        return {
            "ok": True,
            "version": {
                "serverName": "not-codex-control-plane",
                "contractVersion": "1",
                "toolSurfaceHash": "x",
                "guideHash": "y",
                "stableTools": sorted(REQUIRED_STABLE_TOOLS),
            },
        }

    async def scenario() -> None:
        async with CodexControlAdapter(server) as adapter:
            with pytest.raises(CodexContractError, match="unexpected serverName"):
                await adapter.health()

    asyncio.run(scenario())


def test_plan_gate_execute_steer_interrupt_end_to_end() -> None:
    server, state = build_fake_codex()
    memory = MemoryService()
    task = memory.create_task(
        "GAME-501",
        "Single-save implementation",
        repository="cuteyuchen/game",
        goal="Implement a single-save system using the existing StorageManager.",
        hard_constraints=["Do not replace StorageManager."],
    )
    coordinator = CodexCoordinator(memory, lambda: CodexControlAdapter(server))

    async def scenario() -> None:
        initial = memory.get_task(task.task_id)
        started = await coordinator.start_plan(
            task.task_id,
            initial.revision,
            project_id="project-1",
            cwd="C:/dev/game",
        )
        after_start = memory.get_task(task.task_id)
        assert after_start.phase == TaskPhase.PLANNING
        assert after_start.codex_thread_id == "thread-1"
        assert after_start.codex_turn_id == "turn-plan-1"
        assert after_start.revision == initial.revision + 1
        assert started["runtime"]["workflow_id"] == "cwf-1"
        assert started["runtime"]["operation_id"] == "op-plan-1"

        start_call = next(args for tool, args in state["calls"] if tool == "start_plan")
        assert start_call["sandbox"] == "read-only"
        assert "Do not replace StorageManager." in start_call["message"]
        assert start_call["client_request_id"].endswith("plan-v1")

        before_status_revision = after_start.revision
        status = await coordinator.status(task.task_id)
        assert status["remote"]["nextRecommendedAction"] == "review_plan"
        assert memory.get_task(task.task_id).revision == before_status_revision

        imported = await coordinator.import_latest_plan(task.task_id, before_status_revision)
        draft = imported["plan"]
        after_import = memory.get_task(task.task_id)
        assert draft["status"] == "DRAFT"
        assert after_import.phase == TaskPhase.PLAN_REVIEW

        approved = memory.approve_plan(task.task_id, after_import.revision, draft["plan_id"])
        assert approved.status.value == "APPROVED"
        after_approval = memory.get_task(task.task_id)

        executed = await coordinator.execute_approved_plan(
            task.task_id,
            after_approval.revision,
        )
        after_execute = memory.get_task(task.task_id)
        assert after_execute.phase == TaskPhase.IMPLEMENTING
        assert after_execute.codex_turn_id == "turn-exec-1"
        assert executed["runtime"]["operation_id"] == "op-exec-1"
        approve_call = next(args for tool, args in state["calls"] if tool == "approve_plan")
        assert approve_call["sandbox"] == "workspace-write"
        assert approve_call["client_request_id"].endswith("execute:plan-v1")

        steered = await coordinator.soft_steer(
            task.task_id,
            after_execute.revision,
            "Reuse the existing migration helper; do not create a second abstraction.",
        )
        after_steer = memory.get_task(task.task_id)
        assert steered["runtime"]["operation_id"] == "op-steer-1"
        steer_call = next(args for tool, args in state["calls"] if tool == "submit_task")
        assert steer_call["operation_type"] == "steer_turn"
        assert steer_call["thread_id"] == "thread-1"
        assert steer_call["expected_turn_id"] == "turn-exec-1"

        pending = await coordinator.pending_interactions(task.task_id)
        assert pending["remote"]["interactions"][0]["interactionId"] == "interaction-1"
        assert memory.get_task(task.task_id).revision == after_steer.revision

        answered = await coordinator.answer_interaction(
            task.task_id,
            after_steer.revision,
            "interaction-1",
            decision="accept",
        )
        after_answer = memory.get_task(task.task_id)
        assert answered["remote"]["status"] == "answered"

        interrupted = await coordinator.interrupt(
            task.task_id,
            after_answer.revision,
            reason="User requested a hard replan.",
        )
        final_task = memory.get_task(task.task_id)
        assert final_task.phase == TaskPhase.PAUSED
        assert interrupted["runtime"]["remote_status"] == "interrupted"
        assert state["interrupted"] is True
        assert memory.timeline(task.task_id)[-1].event_type == EventType.CODEX_INTERRUPTED

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_execute_refuses_remote_plan_drift() -> None:
    server, state = build_fake_codex()
    memory = MemoryService()
    task = memory.create_task("GAME-DRIFT", "Drift gate", goal="Implement safely")
    coordinator = CodexCoordinator(memory, lambda: CodexControlAdapter(server))

    async def scenario() -> None:
        await coordinator.start_plan(task.task_id, task.revision, project_id="project-1")
        current = memory.get_task(task.task_id)
        imported = await coordinator.import_latest_plan(task.task_id, current.revision)
        current = memory.get_task(task.task_id)
        memory.approve_plan(task.task_id, current.revision, imported["plan"]["plan_id"])
        current = memory.get_task(task.task_id)

        state["plan_markdown"] = "# Plan\n\n1. Replace everything with a new subsystem."
        with pytest.raises(CodexPlanGateError, match="no longer matches"):
            await coordinator.execute_approved_plan(task.task_id, current.revision)

        assert not any(tool == "approve_plan" for tool, _ in state["calls"])
        assert memory.get_task(task.task_id).revision == current.revision

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
