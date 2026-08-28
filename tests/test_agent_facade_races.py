from __future__ import annotations

import asyncio
from typing import Any

import pytest

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    PendingInteraction,
    PlanHandle,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import (
    AgentSafetyState,
    get_agent_safety,
)
from codex_supervisor_bridge.memory.codex_runtime import (
    bind_codex_runtime,
    get_codex_runtime,
)
from codex_supervisor_bridge.memory.execution import (
    acquire_writer,
    get_execution_state,
    handoff_writer,
)
from codex_supervisor_bridge.memory.models import ActiveWriter, EventType
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.workspace import bind_workspace
from codex_supervisor_bridge.supervisor.agent_execution import (
    AgentCompensationRequiredError,
    AgentExecutionCoordinator,
    AgentPlanGateError,
    AgentStaleContextError,
)
from codex_supervisor_bridge.supervisor.agent_facade import AgentSupervisorFacade


class RaceAgent:
    def __init__(
        self,
        *,
        pending: list[PendingInteraction] | None = None,
        block_steer: bool = False,
        block_respond: bool = False,
        block_interrupt: bool = False,
        observe_new_unknown: bool = False,
    ) -> None:
        self.pending = pending or []
        self.block_steer = block_steer
        self.block_respond = block_respond
        self.block_interrupt = block_interrupt
        self.observe_new_unknown = observe_new_unknown
        self.steer_started = asyncio.Event()
        self.respond_started = asyncio.Event()
        self.interrupt_started = asyncio.Event()
        self.release_steer = asyncio.Event()
        self.release_respond = asyncio.Event()
        self.release_interrupt = asyncio.Event()
        self.interrupts: list[PlanHandle] = []
        self.calls: list[str] = []

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="race",
            status=BackendHealthStatus.READY,
            user_message="ready",
        )

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        del task_id, context_pack, workspace
        raise AssertionError("not used")

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return AgentSnapshot(status="completed", **self._identity(handle))

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        del task_id, context_pack, approved_plan, workspace, lease
        raise AssertionError("not used")

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        del cursor, wait_ms
        if self.observe_new_unknown:
            return AgentSnapshot(status="unknown", **self._identity(handle))
        return AgentSnapshot(status="running", **self._identity(handle))

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        del instruction, lease
        self.calls.append("steer")
        self.steer_started.set()
        if self.block_steer:
            await self.release_steer.wait()
        return AgentSnapshot(status="running", **self._identity(handle))

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        self.calls.append("interrupt")
        self.interrupts.append(handle)
        self.interrupt_started.set()
        if self.block_interrupt:
            await self.release_interrupt.wait()
        return AgentSnapshot(status="interrupted", **self._identity(handle))

    async def list_pending_interactions(self, handle: PlanHandle) -> list[PendingInteraction]:
        del handle
        return list(self.pending)

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot:
        del interaction, response
        self.calls.append("respond")
        self.respond_started.set()
        if self.block_respond:
            await self.release_respond.wait()
        return AgentSnapshot(status="running", **self._identity(handle))

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return AgentSnapshot(status="running", **self._identity(handle))

    @staticmethod
    def _identity(handle: PlanHandle) -> dict[str, str | None]:
        return {
            "workflow_id": handle.workflow_id,
            "operation_id": handle.operation_id,
            "thread_id": handle.thread_id,
            "turn_id": handle.turn_id,
        }


def _set_up_codex_writer(
    memory: MemoryService,
    task_id: str,
    *,
    thread_id: str = "thread-old",
    turn_id: str = "turn-old",
    remote_status: str = "executing",
) -> tuple[int, int]:
    task = memory.create_task(task_id, "Race", repository="C:/repo")
    bind_workspace(
        memory.store,
        task.task_id,
        task.revision,
        backend_name="devspace",
        workspace_id="ws-race",
        repository="C:/repo",
        root="C:/worktrees/ws-race",
        workspace_mode="worktree",
    )
    task = memory.get_task(task_id)
    acquired = acquire_writer(
        memory.store,
        task.task_id,
        task.revision,
        ActiveWriter.CODEX,
        explicit_user_authorization=True,
    )
    _, runtime = bind_codex_runtime(
        memory.store,
        task_id,
        acquired.task.revision,
        event_type=EventType.CODEX_STARTED,
        workflow_id="wf-old",
        operation_id="op-old",
        thread_id=thread_id,
        turn_id=turn_id,
        remote_status=remote_status,
    )
    return acquired.execution.writer_epoch, runtime.remote_status or ""


def _facade(
    memory: MemoryService,
    agent: RaceAgent,
) -> AgentSupervisorFacade:
    coordinator = AgentExecutionCoordinator(memory, agent)
    return AgentSupervisorFacade(
        memory,
        coordinator,
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
    )


def test_soft_steer_revision_race_compensates_stale_runtime() -> None:
    memory = MemoryService()
    agent = RaceAgent(block_steer=True)
    try:
        _set_up_codex_writer(memory, "STEER-RACE")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("STEER-RACE")
            pending = asyncio.create_task(
                facade.soft_steer("STEER-RACE", task.revision, "Adjust scope")
            )
            await agent.steer_started.wait()
            memory.update_intent(
                "STEER-RACE",
                task.revision,
                "Hard replan while steering",
            )
            agent.release_steer.set()
            with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
                await pending

        asyncio.run(scenario())
        assert len(agent.interrupts) == 1
        assert agent.interrupts[0].turn_id == "turn-old"
        safety = get_agent_safety(memory.store, "STEER-RACE")
        assert safety is not None and safety.state == AgentSafetyState.NONE
        runtime = get_codex_runtime(memory.store, "STEER-RACE")
        assert runtime is not None and runtime.turn_id == "turn-old"
    finally:
        memory.close()


def test_command_approval_answer_race_compensates_after_writer_handback() -> None:
    memory = MemoryService()
    interaction = PendingInteraction(
        interaction_id="17",
        kind="command_approval",
        summary="Run tests?",
        options=["accept", "decline"],
    )
    agent = RaceAgent(pending=[interaction], block_respond=True)
    try:
        epoch, _ = _set_up_codex_writer(memory, "ANSWER-RACE")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("ANSWER-RACE")
            pending = asyncio.create_task(
                facade.answer_interaction(
                    "ANSWER-RACE",
                    task.revision,
                    "17",
                    decision="accept",
                    scope="turn",
                )
            )
            await agent.respond_started.wait()
            current = memory.get_task("ANSWER-RACE")
            execution = get_execution_state(memory.store, "ANSWER-RACE")
            handed = handoff_writer(
                memory.store,
                "ANSWER-RACE",
                current.revision,
                from_writer=ActiveWriter.CODEX,
                to_writer=ActiveWriter.CHATGPT,
                expected_writer_epoch=execution.writer_epoch,
                reason="Return writer while approval is in flight",
            )
            assert handed.execution.writer_epoch != epoch
            agent.release_respond.set()
            with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
                await pending

        asyncio.run(scenario())
        assert len(agent.interrupts) == 1
        safety = get_agent_safety(memory.store, "ANSWER-RACE")
        assert safety is not None and safety.state == AgentSafetyState.NONE
    finally:
        memory.close()


def test_user_input_answer_is_revision_fenced_but_not_writer_bound() -> None:
    memory = MemoryService()
    interaction = PendingInteraction(
        interaction_id="18",
        kind="user_input",
        summary="Which test?",
    )
    agent = RaceAgent(pending=[interaction], block_respond=True)
    try:
        task = memory.create_task("USER-INPUT-RACE", "User input", repository="C:/repo")
        bind_workspace(
            memory.store,
            task.task_id,
            task.revision,
            backend_name="devspace",
            workspace_id="ws-race",
            repository="C:/repo",
            root="C:/worktrees/ws-race",
            workspace_mode="worktree",
        )
        task = memory.get_task(task.task_id)
        acquired = acquire_writer(
            memory.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
        )
        bind_codex_runtime(
            memory.store,
            task.task_id,
            acquired.task.revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id="wf-user",
            operation_id="op-user",
            thread_id="thread-user",
            turn_id="turn-user",
            remote_status="executing",
        )
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("USER-INPUT-RACE")
            pending = asyncio.create_task(
                facade.answer_interaction(
                    "USER-INPUT-RACE",
                    task.revision,
                    "18",
                    answers={"answer": "unit tests"},
                )
            )
            await agent.respond_started.wait()
            memory.record_user_override(
                "USER-INPUT-RACE",
                memory.get_task("USER-INPUT-RACE").revision,
                "Replan while user input is pending",
            )
            agent.release_respond.set()
            with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
                await pending

        asyncio.run(scenario())
        assert len(agent.interrupts) == 1
        safety = get_agent_safety(memory.store, "USER-INPUT-RACE")
        assert safety is not None and safety.state == AgentSafetyState.NONE
    finally:
        memory.close()


def test_provider_request_interaction_is_denied_by_default() -> None:
    memory = MemoryService()
    interaction = PendingInteraction(
        interaction_id="19",
        kind="provider_request",
        summary="Grant provider access",
    )
    agent = RaceAgent(pending=[interaction])
    try:
        _set_up_codex_writer(memory, "PROVIDER-RACE")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("PROVIDER-RACE")
            with pytest.raises(AgentPlanGateError, match="denied by default"):
                await facade.answer_interaction(
                    "PROVIDER-RACE",
                    task.revision,
                    "19",
                    decision="accept",
                )

        asyncio.run(scenario())
        assert "respond" not in agent.calls
        assert agent.interrupts == []
    finally:
        memory.close()


def test_unknown_interaction_kind_is_denied() -> None:
    memory = MemoryService()
    interaction = PendingInteraction(
        interaction_id="20",
        kind="mystery",
        summary="Unknown request",
    )
    agent = RaceAgent(pending=[interaction])
    try:
        _set_up_codex_writer(memory, "UNKNOWN-RACE")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("UNKNOWN-RACE")
            with pytest.raises(AgentPlanGateError, match="unsupported interaction kind"):
                await facade.answer_interaction(
                    "UNKNOWN-RACE",
                    task.revision,
                    "20",
                    decision="accept",
                )

        asyncio.run(scenario())
        assert "respond" not in agent.calls
    finally:
        memory.close()


def test_stale_interrupt_does_not_overwrite_new_runtime() -> None:
    memory = MemoryService()
    agent = RaceAgent(block_interrupt=True)
    try:
        _set_up_codex_writer(memory, "INTERRUPT-RACE")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("INTERRUPT-RACE")
            pending = asyncio.create_task(
                facade.interrupt("INTERRUPT-RACE", task.revision, reason="Scope check")
            )
            await agent.interrupt_started.wait()
            current = memory.get_task("INTERRUPT-RACE")
            bind_codex_runtime(
                memory.store,
                "INTERRUPT-RACE",
                current.revision,
                event_type=EventType.CODEX_STARTED,
                workflow_id="wf-new",
                operation_id="op-new",
                thread_id="thread-new",
                turn_id="turn-new",
                remote_status="executing",
            )
            agent.release_interrupt.set()
            result = await pending
            assert result["stale_runtime_interrupted"] is True
            runtime = get_codex_runtime(memory.store, "INTERRUPT-RACE")
            assert runtime is not None
            assert runtime.thread_id == "thread-new"
            assert runtime.turn_id == "turn-new"

        asyncio.run(scenario())
        safety = get_agent_safety(memory.store, "INTERRUPT-RACE")
        assert safety is not None and safety.state == AgentSafetyState.NONE
    finally:
        memory.close()


def test_stale_interrupt_with_unconfirmable_new_runtime_fails_closed() -> None:
    memory = MemoryService()
    agent = RaceAgent(
        block_interrupt=True,
        observe_new_unknown=True,
    )
    try:
        _set_up_codex_writer(memory, "INTERRUPT-UNKNOWN")
        facade = _facade(memory, agent)

        async def scenario() -> None:
            task = memory.get_task("INTERRUPT-UNKNOWN")
            pending = asyncio.create_task(
                facade.interrupt(
                    "INTERRUPT-UNKNOWN",
                    task.revision,
                    reason="Scope check",
                )
            )
            await agent.interrupt_started.wait()
            current = memory.get_task("INTERRUPT-UNKNOWN")
            bind_codex_runtime(
                memory.store,
                "INTERRUPT-UNKNOWN",
                current.revision,
                event_type=EventType.CODEX_STARTED,
                workflow_id="wf-new",
                operation_id="op-new",
                thread_id="thread-new",
                turn_id="turn-new",
                remote_status="executing",
            )
            agent.release_interrupt.set()
            with pytest.raises(AgentCompensationRequiredError, match="COMPENSATION_REQUIRED"):
                await pending

        asyncio.run(scenario())
        safety = get_agent_safety(memory.store, "INTERRUPT-UNKNOWN")
        assert safety is not None
        assert safety.state == AgentSafetyState.RECONCILIATION_REQUIRED
    finally:
        memory.close()
