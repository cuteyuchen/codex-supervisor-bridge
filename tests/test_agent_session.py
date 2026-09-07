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
from codex_supervisor_bridge.memory.agent_safety import (
    get_agent_safety,
    record_agent_compensation_required,
)
from codex_supervisor_bridge.memory.backend_binding import bind_task_backend
from codex_supervisor_bridge.memory.codex_runtime import (
    bind_codex_runtime,
    get_codex_runtime,
)
from codex_supervisor_bridge.memory.execution import acquire_writer, get_execution_state
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType, TaskPhase
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_session import AgentSessionManager


def fake_bridge(
    *,
    resumable: bool = True,
    runtime_status: str | None = None,
) -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-local-codex-bridge")
    state: dict[str, Any] = {
        "calls": [],
        "resumable": resumable,
        "runtime_status": runtime_status,
    }

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
        if state["runtime_status"] is not None:
            return {
                "runtime_status": state["runtime_status"],
                "thread_id": thread_id,
            }
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


def test_recovery_is_scoped_to_current_composition_binding(tmp_path: Path) -> None:
    database = tmp_path / "binding-scope.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("BINDING-SCOPE", "Bound", repository="C:/repo")
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CODEX,
            explicit_user_authorization=True,
        )
        bind_task_backend(
            memory.store,
            task.task_id,
            acquired.task.revision,
            workspace_backend="kandev",
            agent_backend="control_plane",
            profile="existing",
        )
        task = memory.get_task(task.task_id)
        bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-bound",
            operation_id="op-bound",
            thread_id="thread-bound",
            turn_id="turn-bound",
            remote_status="executing",
        )
    finally:
        memory.close()

    class ScopedProbeAgent:
        def __init__(self) -> None:
            self.resume_calls = 0

        async def __aenter__(self) -> "ScopedProbeAgent":
            return self

        async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
            return None

        async def resume(self, handle: Any) -> Any:
            self.resume_calls += 1
            return handle

    reopened = MemoryService(database)
    agent = ScopedProbeAgent()
    try:
        session = AgentSessionManager(
            reopened,
            lambda: agent,
            profile="lightweight",
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
        )

        async def scenario() -> None:
            outcomes = await session.start()
            assert [outcome.status for outcome in outcomes] == [
                "RECONCILIATION_REQUIRED"
            ]
            assert "different backend profile" in outcomes[0].detail

        asyncio.run(scenario())
        assert agent.resume_calls == 0
    finally:
        asyncio.run(session.shutdown())
        reopened.close()


def test_plan_mode_restart_resumes_planning_runtime_without_writer(
    tmp_path: Path,
) -> None:
    database = tmp_path / "plan-restart.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("PLAN-RESTART", "Plan", repository="C:/repo")
        bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        task = memory.get_task(task.task_id)
        bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-plan",
            operation_id="op-plan",
            thread_id="thread-plan",
            turn_id="turn-plan",
            remote_status="planning",
        )
    finally:
        memory.close()

    reopened = MemoryService(database)
    server, state = fake_bridge(resumable=True)
    session = AgentSessionManager(
        reopened,
        lambda: LocalCodexBridgeAgentBackend(server),
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    try:
        async def scenario() -> None:
            try:
                outcomes = await session.start()
                assert [outcome.status for outcome in outcomes] == ["RESUMED"]
                assert get_agent_safety(reopened.store, "PLAN-RESTART") is None
            finally:
                await session.shutdown()

        asyncio.run(scenario())
        assert [call[0] for call in state["calls"]].count("observe") == 1
    finally:
        reopened.close()


def test_plan_mode_restart_preserves_chatgpt_writer(tmp_path: Path) -> None:
    database = tmp_path / "plan-chatgpt-restart.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task(
            "PLAN-CHATGPT",
            "Plan while ChatGPT writes",
            repository="C:/repo",
        )
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
        )
        bind_task_backend(
            memory.store,
            task.task_id,
            acquired.task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        task = memory.get_task(task.task_id)
        bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-plan-chatgpt",
            operation_id="op-plan-chatgpt",
            thread_id="thread-plan-chatgpt",
            turn_id="turn-plan-chatgpt",
            remote_status="inProgress",
            task_phase=TaskPhase.PLANNING,
        )
        expected_epoch = acquired.execution.writer_epoch
    finally:
        memory.close()

    reopened = MemoryService(database)
    server, state = fake_bridge(resumable=True)
    session = AgentSessionManager(
        reopened,
        lambda: LocalCodexBridgeAgentBackend(server),
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    try:
        async def scenario() -> None:
            try:
                outcomes = await session.start()
                assert [outcome.status for outcome in outcomes] == ["RESUMED"]
            finally:
                await session.shutdown()

        asyncio.run(scenario())
        execution = get_execution_state(reopened.store, "PLAN-CHATGPT")
        assert execution.active_writer == ActiveWriter.CHATGPT
        assert execution.writer_epoch == expected_epoch
        assert [call[0] for call in state["calls"]].count("observe") == 1
    finally:
        reopened.close()


def test_non_reconstructable_read_only_plan_clears_legacy_recovery_latch(
    tmp_path: Path,
) -> None:
    database = tmp_path / "plan-not-reconstructable.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task(
            "PLAN-NOT-RECONSTRUCTABLE",
            "Plan restart",
            repository="C:/repo",
        )
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
        )
        bind_task_backend(
            memory.store,
            task.task_id,
            acquired.task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        task = memory.get_task(task.task_id)
        task, runtime = bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            thread_id="thread-plan-stale",
            turn_id="turn-plan-stale",
            remote_status="inProgress",
            task_phase=TaskPhase.PLANNING,
        )
        record_agent_compensation_required(
            memory.store,
            task.task_id,
            operation="runtime_recovery",
            summary="Misclassified planning runtime.",
            details={
                "recovery_reason": (
                    "workspace-write runtime requires an active CODEX writer lease"
                )
            },
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
        )
    finally:
        memory.close()

    reopened = MemoryService(database)
    server, state = fake_bridge(runtime_status="not_reconstructable")
    session = AgentSessionManager(
        reopened,
        lambda: LocalCodexBridgeAgentBackend(server),
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    try:
        async def scenario() -> None:
            try:
                outcomes = await session.start()
                assert [outcome.status for outcome in outcomes] == [
                    "PLAN_RESTART_REQUIRED"
                ]
            finally:
                await session.shutdown()

        asyncio.run(scenario())
        safety = get_agent_safety(reopened.store, "PLAN-NOT-RECONSTRUCTABLE")
        assert safety is not None and safety.state == "NONE"
        task = reopened.get_task("PLAN-NOT-RECONSTRUCTABLE")
        assert task.phase == TaskPhase.PAUSED
        runtime = get_codex_runtime(reopened.store, task.task_id)
        assert runtime is not None and runtime.remote_status == "not_reconstructable"
        execution = get_execution_state(reopened.store, task.task_id)
        assert execution.active_writer == ActiveWriter.CHATGPT
        assert [call[0] for call in state["calls"]].count("observe") == 1
    finally:
        reopened.close()


def test_plan_mode_restart_unknown_resume_fails_closed(tmp_path: Path) -> None:
    database = tmp_path / "plan-unknown-restart.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("PLAN-UNKNOWN", "Plan", repository="C:/repo")
        bind_task_backend(
            memory.store,
            task.task_id,
            task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        task = memory.get_task(task.task_id)
        bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-plan-unknown",
            operation_id="op-plan-unknown",
            thread_id="thread-plan-unknown",
            turn_id="turn-plan-unknown",
            remote_status="planning",
        )
    finally:
        memory.close()

    reopened = MemoryService(database)
    server, state = fake_bridge(resumable=False)
    session = AgentSessionManager(
        reopened,
        lambda: LocalCodexBridgeAgentBackend(server),
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    try:
        async def scenario() -> None:
            try:
                outcomes = await session.start()
                assert [outcome.status for outcome in outcomes] == [
                    "RECONCILIATION_REQUIRED"
                ]
            finally:
                await session.shutdown()

        asyncio.run(scenario())
        safety = get_agent_safety(reopened.store, "PLAN-UNKNOWN")
        assert safety is not None
        assert safety.state == "RECONCILIATION_REQUIRED"
        assert [call[0] for call in state["calls"]].count("observe") == 1
    finally:
        reopened.close()


def test_execution_restart_requires_current_codex_writer_lease(
    tmp_path: Path,
) -> None:
    database = tmp_path / "execution-writer-fence.db"
    memory = MemoryService(database)
    try:
        task = memory.create_task("EXEC-FENCE", "Execution", repository="C:/repo")
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
        )
        bind_task_backend(
            memory.store,
            task.task_id,
            acquired.task.revision,
            workspace_backend="devspace",
            agent_backend="local_codex_bridge",
            profile="lightweight",
        )
        task = memory.get_task(task.task_id)
        bind_codex_runtime(
            memory.store,
            task.task_id,
            task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-exec-fence",
            operation_id="op-exec-fence",
            thread_id="thread-exec-fence",
            turn_id="turn-exec-fence",
            remote_status="executing",
        )
    finally:
        memory.close()

    reopened = MemoryService(database)
    server, state = fake_bridge(resumable=True)
    session = AgentSessionManager(
        reopened,
        lambda: LocalCodexBridgeAgentBackend(server),
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )
    try:
        async def scenario() -> None:
            try:
                outcomes = await session.start()
                assert [outcome.status for outcome in outcomes] == [
                    "RECONCILIATION_REQUIRED"
                ]
            finally:
                await session.shutdown()

        asyncio.run(scenario())
        safety = get_agent_safety(reopened.store, "EXEC-FENCE")
        assert safety is not None
        assert safety.state == "RECONCILIATION_REQUIRED"
        assert state["calls"] == []
    finally:
        reopened.close()


def _counting_client(counts: dict[str, int], server: MCPServer) -> Any:
    from mcp import Client

    counts["created"] += 1
    return Client(server)
