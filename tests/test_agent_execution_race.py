from __future__ import annotations

import asyncio

import pytest

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.agent_safety import (
    AgentSafetyState,
    get_agent_safety,
)
from codex_supervisor_bridge.memory.codex_runtime import get_codex_runtime
from codex_supervisor_bridge.memory.errors import ConflictError
from codex_supervisor_bridge.memory.execution import acquire_writer, handoff_writer, release_writer
from codex_supervisor_bridge.memory.models import ActiveWriter
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.supervisor.agent_execution import (
    AgentCompensationRequiredError,
    AgentExecutionCoordinator,
    AgentPlanGateError,
    AgentStaleContextError,
)


class BlockingAgent:
    def __init__(
        self,
        *,
        block_plan: bool = False,
        block_execution: bool = False,
        interrupt_status: str = "interrupted",
        interrupt_error: bool = False,
    ) -> None:
        self.block_plan = block_plan
        self.block_execution = block_execution
        self.interrupt_status = interrupt_status
        self.interrupt_error = interrupt_error
        self.plan_started = asyncio.Event()
        self.execution_started = asyncio.Event()
        self.release_plan = asyncio.Event()
        self.release_execution = asyncio.Event()
        self.interrupts: list[PlanHandle] = []
        self.execution_calls = 0

    async def health(self) -> BackendHealth:
        return BackendHealth(
            capability="fake",
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
        self.plan_started.set()
        if self.block_plan:
            await self.release_plan.wait()
        return PlanHandle(
            workflow_id="wf-plan-race",
            operation_id="op-plan-race",
            thread_id="thread-plan-race",
            turn_id="turn-plan-race",
            status="planning",
        )

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return AgentSnapshot(
            status="completed",
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            plan=PlanResult(content="1. Implement\n2. Test"),
        )

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
        self.execution_calls += 1
        self.execution_started.set()
        if self.block_execution:
            await self.release_execution.wait()
        return PlanHandle(
            workflow_id="wf-plan-race",
            operation_id="op-execution-race",
            thread_id="thread-plan-race",
            turn_id="turn-execution-race",
            status="executing",
        )

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        del cursor, wait_ms
        return AgentSnapshot(
            status="running",
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        del instruction, lease
        return await self.observe(handle)

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        self.interrupts.append(handle)
        if self.interrupt_error:
            raise RuntimeError("interrupt transport failed")
        return AgentSnapshot(
            status=self.interrupt_status,
            reconciliation_required=self.interrupt_status.upper() == "UNKNOWN",
            workflow_id=handle.workflow_id,
            operation_id=handle.operation_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        )

    async def list_pending_interactions(self, handle: PlanHandle) -> list[object]:
        del handle
        return []

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: object,
        response: dict[str, object],
    ) -> AgentSnapshot:
        del interaction, response
        return await self.observe(handle)

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)


def _workspace() -> WorkspaceState:
    return WorkspaceState(workspace_id="ws-race", repository="C:/repo", root="C:/repo")


async def _prepare_approved_execution(
    memory: MemoryService,
    agent: BlockingAgent,
) -> tuple[AgentExecutionCoordinator, str, PlanHandle, WriterLeaseToken]:
    task = memory.create_task("AGENT-RACE", "Agent race", repository="C:/repo")
    acquired = acquire_writer(
        memory.store,
        task.task_id,
        task.revision,
        ActiveWriter.CODEX,
        explicit_user_authorization=True,
    )
    coordinator = AgentExecutionCoordinator(memory, agent)
    plan_handle = await coordinator.start_plan(
        task.task_id,
        acquired.task.revision,
        context_pack="goal",
        workspace=_workspace(),
    )
    snapshot = await agent.get_plan_status(plan_handle)
    assert snapshot.plan is not None
    draft = coordinator.import_plan(
        task.task_id,
        memory.get_task(task.task_id).revision,
        snapshot,
    )
    approved = memory.approve_plan(
        task.task_id,
        memory.get_task(task.task_id).revision,
        draft.plan_id,
    )
    current = memory.get_task(task.task_id)
    lease = WriterLeaseToken(
        task_id=task.task_id,
        writer=ActiveWriter.CODEX,
        writer_epoch=acquired.execution.writer_epoch,
        task_revision=current.revision,
    )
    return coordinator, approved.plan_id, plan_handle, lease


def test_plan_revision_race_compensates_without_binding_runtime() -> None:
    memory = MemoryService()
    agent = BlockingAgent(block_plan=True)
    coordinator = AgentExecutionCoordinator(memory, agent)
    task = memory.create_task("PLAN-RACE", "Plan race", goal="Original")

    async def scenario() -> None:
        pending = asyncio.create_task(
            coordinator.start_plan(
                task.task_id,
                task.revision,
                context_pack="goal",
                workspace=_workspace(),
            )
        )
        await agent.plan_started.wait()
        memory.update_intent(task.task_id, task.revision, "Updated while planning")
        agent.release_plan.set()
        with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
            await pending

    try:
        asyncio.run(scenario())
        assert len(agent.interrupts) == 1
        assert get_codex_runtime(memory.store, task.task_id) is None
        assert get_agent_safety(memory.store, task.task_id).state == AgentSafetyState.NONE  # type: ignore[union-attr]
        assert any(
            event.event_type.value == "AGENT_COMPENSATION_SUCCEEDED"
            for event in memory.timeline(task.task_id)
        )
    finally:
        memory.close()


def test_execution_revision_race_interrupts_before_implementing_binding() -> None:
    memory = MemoryService()
    agent = BlockingAgent(block_execution=True)

    async def scenario() -> None:
        coordinator, plan_id, plan_handle, lease = await _prepare_approved_execution(memory, agent)
        task = memory.get_task("AGENT-RACE")
        pending = asyncio.create_task(
            coordinator.start_execution(
                task.task_id,
                task.revision,
                plan_id=plan_id,
                plan_handle=plan_handle,
                plan_result=PlanResult(content="1. Implement\n2. Test"),
                context_pack="goal",
                workspace=_workspace(),
                lease=lease,
            )
        )
        await agent.execution_started.wait()
        memory.update_intent(task.task_id, task.revision, "Hard replan while executing")
        agent.release_execution.set()
        with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
            await pending
        runtime = get_codex_runtime(memory.store, task.task_id)
        assert runtime is not None and runtime.remote_status == "planning"
        assert agent.execution_calls == 1
        assert len(agent.interrupts) == 1

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


def test_execution_writer_epoch_race_compensates_stale_lease() -> None:
    memory = MemoryService()
    agent = BlockingAgent(block_execution=True)

    async def scenario() -> None:
        coordinator, plan_id, plan_handle, lease = await _prepare_approved_execution(memory, agent)
        task = memory.get_task("AGENT-RACE")
        pending = asyncio.create_task(
            coordinator.start_execution(
                task.task_id,
                task.revision,
                plan_id=plan_id,
                plan_handle=plan_handle,
                plan_result=PlanResult(content="1. Implement\n2. Test"),
                context_pack="goal",
                workspace=_workspace(),
                lease=lease,
            )
        )
        await agent.execution_started.wait()
        released = release_writer(
            memory.store,
            task.task_id,
            memory.get_task(task.task_id).revision,
            ActiveWriter.CODEX,
            lease.writer_epoch,
        )
        reacquired = acquire_writer(
            memory.store,
            task.task_id,
            released.task.revision,
            ActiveWriter.CODEX,
            explicit_user_authorization=True,
        )
        assert reacquired.execution.writer_epoch != lease.writer_epoch
        agent.release_execution.set()
        with pytest.raises(AgentStaleContextError, match="STALE_CONTEXT"):
            await pending
        assert len(agent.interrupts) == 1

    try:
        asyncio.run(scenario())
    finally:
        memory.close()


@pytest.mark.parametrize("mode", ["unknown", "failure"])
def test_failed_or_unknown_compensation_is_durable_and_blocks_follow_up_writes(mode: str) -> None:
    memory = MemoryService()
    agent = BlockingAgent(
        block_execution=True,
        interrupt_status="UNKNOWN" if mode == "unknown" else "interrupted",
        interrupt_error=mode == "failure",
    )

    async def scenario() -> None:
        coordinator, plan_id, plan_handle, lease = await _prepare_approved_execution(memory, agent)
        task = memory.get_task("AGENT-RACE")
        pending = asyncio.create_task(
            coordinator.start_execution(
                task.task_id,
                task.revision,
                plan_id=plan_id,
                plan_handle=plan_handle,
                plan_result=PlanResult(content="1. Implement\n2. Test"),
                context_pack="goal",
                workspace=_workspace(),
                lease=lease,
            )
        )
        await agent.execution_started.wait()
        memory.record_user_override(task.task_id, task.revision, "Stop this execution")
        agent.release_execution.set()
        with pytest.raises(AgentCompensationRequiredError, match="COMPENSATION_REQUIRED"):
            await pending

        safety = get_agent_safety(memory.store, task.task_id)
        assert safety is not None
        assert safety.state == AgentSafetyState.RECONCILIATION_REQUIRED
        assert "RECONCILIATION REQUIRED" in memory.get_context_pack(task.task_id).content
        current = memory.get_task(task.task_id)
        with pytest.raises(ConflictError, match="compensation requires reconciliation"):
            handoff_writer(
                memory.store,
                task.task_id,
                current.revision,
                from_writer=ActiveWriter.CODEX,
                to_writer=ActiveWriter.CHATGPT,
                expected_writer_epoch=lease.writer_epoch,
                reason="Attempt handback while compensation is unresolved",
            )
        with pytest.raises(AgentPlanGateError, match="compensation requires reconciliation"):
            await coordinator.start_execution(
                task.task_id,
                current.revision,
                plan_id=plan_id,
                plan_handle=plan_handle,
                plan_result=PlanResult(content="1. Implement\n2. Test"),
                context_pack="goal",
                workspace=_workspace(),
                lease=lease,
            )

    try:
        asyncio.run(scenario())
    finally:
        memory.close()
