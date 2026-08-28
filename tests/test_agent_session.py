from __future__ import annotations

import asyncio
from pathlib import Path
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
from codex_supervisor_bridge.memory.agent_safety import get_agent_safety
from codex_supervisor_bridge.memory.codex_runtime import bind_codex_runtime
from codex_supervisor_bridge.memory.execution import acquire_writer
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_session import AgentSessionManager


def fake_bridge(*, resumable: bool = True) -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-local-codex-bridge")
    state: dict[str, Any] = {"calls": [], "resumable": resumable}

    @server.tool()
    def codex_turn(
        text: str,
        thread_id: str | None = None,
        cwd: str | None = None,
        sandbox: str | None = None,
        approval_policy: str | None = None,
    ) -> dict[str, Any]:
        state["calls"].append(("turn", thread_id, cwd, sandbox, approval_policy))
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
        if not state["resumable"]:
            return {"runtime_status": "unknown", "thread_id": thread_id}
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
        state["calls"].append(("respond", request_id, thread_id, method, turn_id, decision, answers, scope))
        return {"status": "inProgress", "thread_id": thread_id, "turn_id": turn_id}

    @server.tool()
    def codex_interrupt(thread_id: str, turn_id: str) -> dict[str, Any]:
        state["calls"].append(("interrupt", thread_id, turn_id))
        return {"status": "interrupted", "thread_id": thread_id, "turn_id": turn_id}

    return server, state


def lease() -> WriterLeaseToken:
    return WriterLeaseToken(
        task_id="SESSION-1",
        writer=ActiveWriter.CODEX,
        writer_epoch=2,
        task_revision=4,
    )


def test_lcb_turn_lifecycle_reuses_one_persistent_session() -> None:
    server, state = fake_bridge()
    counts = {"created": 0}
    memory = MemoryService()
    try:

        def backend_factory() -> LocalCodexBridgeAgentBackend:
            return LocalCodexBridgeAgentBackend(
                server,
                client_factory=lambda: _counting_client(counts, server),
            )

        session = AgentSessionManager(
            memory,
            backend_factory,
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
        )

        async def scenario() -> None:
            await session.start()
            health = await session.health()
            assert health.status == BackendHealthStatus.READY
            handle = await session.start_plan(
                task_id="SESSION-1",
                context_pack="Goal: test",
                workspace=WorkspaceState(
                    workspace_id="ws",
                    repository="C:/repo",
                    root="C:/repo",
                ),
            )
            assert handle.thread_id == "thread-1"
            observed = await session.observe(handle)
            assert observed.pending_interactions
            steered = await session.steer(handle, "Run tests.", lease=lease())
            assert steered.status == "inProgress"
            interaction = observed.pending_interactions[0]
            answered = await session.respond_interaction(
                handle,
                interaction,
                {"decision": "accept", "scope": "turn"},
            )
            assert answered.status == "inProgress"
            interrupted = await session.interrupt(handle)
            assert interrupted.status == "interrupted"
            await session.shutdown()

        asyncio.run(scenario())
        assert counts["created"] == 1
        assert session.session_count == 1
        assert session.shutdown_count == 1
        assert any(call[0] == "steer" for call in state["calls"])
        assert any(call[0] == "respond" for call in state["calls"])
        assert session.connected is False
    finally:
        memory.close()


def test_restart_resume_confirmed_or_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "session-restart.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("SESSION-RESTART", "Restart", repository="C:/repo")
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CODEX,
            explicit_user_authorization=True,
        )
        bind_codex_runtime(
            memory.store,
            task.task_id,
            acquired.task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-restart",
            operation_id="op-restart",
            thread_id="thread-1",
            turn_id="turn-1",
            remote_status="executing",
            task_phase=TaskPhase.IMPLEMENTING,
            current_state="Codex is implementing.",
        )
    finally:
        memory.close()

    async def inspect_restart(resumable: bool) -> str | None:
        reopened = MemoryService(database)
        server, _ = fake_bridge(resumable=resumable)
        session = AgentSessionManager(
            reopened,
            lambda: LocalCodexBridgeAgentBackend(server),
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
        )
        try:
            await session.start()
            safety = get_agent_safety(reopened.store, "SESSION-RESTART")
            return None if safety is None else safety.state
        finally:
            await session.shutdown()
            reopened.close()

    resumed = asyncio.run(inspect_restart(resumable=True))
    assert resumed in {None, "NONE"}
    reconciled = asyncio.run(inspect_restart(resumable=False))
    assert reconciled == "RECONCILIATION_REQUIRED"


def _counting_client(counts: dict[str, int], server: MCPServer) -> Any:
    from mcp import Client

    counts["created"] += 1
    return Client(server)
