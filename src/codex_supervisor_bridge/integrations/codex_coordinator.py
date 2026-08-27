from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from codex_supervisor_bridge.memory.codex_runtime import (
    CodexRuntimeState,
    bind_codex_runtime,
    get_codex_runtime,
)
from codex_supervisor_bridge.memory.models import (
    Actor,
    ContextPackMode,
    EventType,
    Plan,
    PlanStatus,
    TaskPhase,
)
from codex_supervisor_bridge.memory.service import MemoryService

from .codex_control_client import CodexControlAdapter
from .codex_control_errors import CodexPlanGateError

AdapterFactory = Callable[[], CodexControlAdapter]


def _string(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _nested_string(payload: dict[str, Any], key: str, *nested_keys: str) -> str | None:
    nested = payload.get(key)
    if not isinstance(nested, dict):
        return None
    return _string(nested, *nested_keys)


def _latest_plan(payload: dict[str, Any]) -> tuple[str, str | None, str | None]:
    latest = payload.get("latestPlan")
    if not isinstance(latest, dict):
        raise CodexPlanGateError("Codex workflow has no latestPlan to review")
    quality = _string(latest, "planQuality", "quality")
    if quality != "valid_plan":
        raise CodexPlanGateError(
            f"latestPlan is not a valid_plan (quality={quality!r})"
        )
    markdown = _string(latest, "markdown", "content", "plan")
    if not markdown:
        raise CodexPlanGateError("latestPlan has no plan markdown/content")
    plan_hash = _string(latest, "planHash", "hash")
    turn_id = _string(latest, "turnId", "turn_id")
    return markdown.strip(), plan_hash, turn_id


def _normalized_plan(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class CodexCoordinator:
    """Supervisor-owned semantic control over Codex Control Plane MCP."""

    def __init__(self, memory: MemoryService, adapter_factory: AdapterFactory) -> None:
        self.memory = memory
        self.adapter_factory = adapter_factory

    async def health(self) -> dict[str, Any]:
        async with self.adapter_factory() as adapter:
            return await adapter.health()

    async def runtime_capabilities(self) -> dict[str, Any]:
        async with self.adapter_factory() as adapter:
            return await adapter.runtime_capabilities()

    async def preflight(
        self,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]:
        async with self.adapter_factory() as adapter:
            return await adapter.preflight(
                project_id=project_id,
                cwd=cwd,
                model=model,
                workflow_kind=workflow_kind,
            )

    def runtime_state(self, task_id: str) -> CodexRuntimeState | None:
        return get_codex_runtime(self.memory.store, task_id)

    @staticmethod
    def _plan_request_id(task_id: str, intent_version: int, next_plan_version: int) -> str:
        return (
            f"codex-supervisor-bridge:{task_id}:plan:"
            f"intent-v{intent_version}:plan-v{next_plan_version}"
        )

    async def start_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        project_id: str,
        cwd: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        task = self.memory.assert_revision(task_id, expected_revision)
        pack = self.memory.get_context_pack(task_id, mode=ContextPackMode.PLAN_REVIEW)
        client_request_id = self._plan_request_id(
            task_id,
            task.intent_version,
            task.plan_version + 1,
        )
        message = (
            "Analyze the repository in Plan Mode. Do not implement or modify files yet. "
            "Produce a concrete implementation plan that satisfies the canonical Supervisor "
            "context below.\n\n"
            + pack.content
        )
        args: dict[str, Any] = {
            "project_id": project_id,
            "message": message,
            "title": f"{task.task_id}: {task.title}",
            "sandbox": "read-only",
            "approval_policy": "on-request",
            "client_request_id": client_request_id,
            "goal": task.current_goal,
        }
        if cwd:
            args["cwd"] = cwd
        if model:
            args["model"] = model

        async with self.adapter_factory() as adapter:
            remote = await adapter.start_plan_workflow(args)

        workflow_id = _string(remote, "workflowId", "workflow_id")
        if not workflow_id:
            raise CodexPlanGateError("Plan workflow start returned no workflowId")
        thread_id = _string(remote, "threadId", "thread_id")
        turn_id = _string(remote, "planTurnId", "turnId", "turn_id")
        operation_id = (
            _string(remote, "planOperationId", "operationId", "operation_id")
            or _nested_string(remote, "startOperation", "operationId", "operation_id")
        )
        status = _string(remote, "status", "phase") or "planning"
        next_action = _string(remote, "nextRecommendedAction")
        current, runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id=workflow_id,
            operation_id=operation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            remote_status=status,
            next_action=next_action,
            client_request_id=client_request_id,
            task_phase=TaskPhase.PLANNING,
            current_state="Codex is producing a read-only implementation plan.",
            event_payload={"mode": "plan"},
        )
        return {
            "task": current.model_dump(mode="json"),
            "runtime": runtime.model_dump(mode="json"),
            "remote": remote,
        }

    async def status(self, task_id: str) -> dict[str, Any]:
        task = self.memory.get_task(task_id)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            return {
                "task": task.model_dump(mode="json"),
                "runtime": None,
                "remote": None,
                "pollRecommended": False,
            }
        async with self.adapter_factory() as adapter:
            if runtime.workflow_id:
                remote = await adapter.get_workflow_status(runtime.workflow_id)
            elif runtime.operation_id:
                remote = await adapter.get_operation_status(runtime.operation_id)
            else:
                remote = None
        return {
            "task": task.model_dump(mode="json"),
            "runtime": runtime.model_dump(mode="json"),
            "remote": remote,
            "pollRecommended": bool(remote and remote.get("pollRecommended", False)),
        }

    async def import_latest_plan(
        self,
        task_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not runtime.workflow_id:
            raise CodexPlanGateError("Task has no active Codex plan workflow")
        async with self.adapter_factory() as adapter:
            remote = await adapter.get_workflow_status(runtime.workflow_id)
        markdown, plan_hash, remote_turn_id = _latest_plan(remote)
        plan = self.memory.store.create_plan(
            task_id,
            expected_revision,
            markdown,
            actor=Actor.CODEX,
        )
        task = self.memory.get_task(task_id)
        return {
            "task": task.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
            "remotePlanHash": plan_hash,
            "remotePlanTurnId": remote_turn_id,
            "nextRecommendedAction": remote.get("nextRecommendedAction"),
        }

    async def execute_approved_plan(
        self,
        task_id: str,
        expected_revision: int,
        *,
        sandbox: str = "workspace-write",
    ) -> dict[str, Any]:
        task = self.memory.assert_revision(task_id, expected_revision)
        approved = self.memory.approved_plan(task_id)
        if approved is None or approved.status != PlanStatus.APPROVED:
            raise CodexPlanGateError("No locally APPROVED plan exists")
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not runtime.workflow_id:
            raise CodexPlanGateError("Task has no Codex plan workflow to execute")

        async with self.adapter_factory() as adapter:
            before = await adapter.get_workflow_status(runtime.workflow_id)
            remote_plan, _, _ = _latest_plan(before)
            if _normalized_plan(remote_plan) != _normalized_plan(approved.content):
                raise CodexPlanGateError(
                    "Locally approved plan no longer matches Codex latestPlan; re-import and review"
                )
            client_request_id = (
                f"codex-supervisor-bridge:{task_id}:execute:plan-v{approved.plan_version}"
            )
            remote = await adapter.approve_plan(
                {
                    "workflow_id": runtime.workflow_id,
                    "client_request_id": client_request_id,
                    "message": "Implement the approved plan exactly. Respect all active constraints.",
                    "approval_policy": "on-request",
                    "sandbox": sandbox,
                }
            )

        operation_id = _string(remote, "executionOperationId", "operationId", "operation_id")
        thread_id = _string(remote, "threadId", "thread_id") or runtime.thread_id
        turn_id = _string(remote, "executionTurnId", "turnId", "turn_id") or runtime.turn_id
        status = _string(remote, "status", "phase") or "executing"
        next_action = _string(remote, "nextRecommendedAction")
        current, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STARTED,
            workflow_id=runtime.workflow_id,
            operation_id=operation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            remote_status=status,
            next_action=next_action,
            client_request_id=client_request_id,
            task_phase=TaskPhase.IMPLEMENTING,
            current_state="Codex is implementing the approved plan.",
            event_payload={"mode": "execute", "plan_id": approved.plan_id},
        )
        return {
            "task": current.model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "remote": remote,
        }

    async def soft_steer(
        self,
        task_id: str,
        expected_revision: int,
        instruction: str,
    ) -> dict[str, Any]:
        if not instruction.strip():
            raise ValueError("instruction must not be empty")
        self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise CodexPlanGateError("Task has no active Codex runtime")

        thread_id = runtime.thread_id
        turn_id = runtime.turn_id
        if not thread_id or not turn_id:
            live = await self.status(task_id)
            remote = live.get("remote")
            if isinstance(remote, dict):
                thread_id = _string(remote, "threadId", "thread_id") or thread_id
                turn_id = _string(
                    remote,
                    "turnId",
                    "executionTurnId",
                    "planTurnId",
                    "turn_id",
                ) or turn_id
        if not thread_id or not turn_id:
            raise CodexPlanGateError("Cannot steer: current Codex thread/turn is unknown")

        digest = hashlib.sha256(instruction.strip().encode("utf-8")).hexdigest()[:12]
        client_request_id = (
            f"codex-supervisor-bridge:{task_id}:steer:r{expected_revision}:{digest}"
        )
        async with self.adapter_factory() as adapter:
            remote = await adapter.steer_turn(
                thread_id=thread_id,
                expected_turn_id=turn_id,
                message=instruction.strip(),
                client_request_id=client_request_id,
            )
        operation_id = _string(remote, "operationId", "operation_id")
        current, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_STEERED,
            workflow_id=runtime.workflow_id,
            operation_id=operation_id or runtime.operation_id,
            thread_id=thread_id,
            turn_id=turn_id,
            remote_status=_string(remote, "status") or "running",
            next_action=_string(remote, "nextRecommendedAction"),
            client_request_id=client_request_id,
            current_state="Codex accepted a supervisor steering instruction.",
            event_payload={"instruction": instruction.strip()},
        )
        return {
            "task": current.model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "remote": remote,
        }

    async def interrupt(
        self,
        task_id: str,
        expected_revision: int,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None or not any(
            [runtime.workflow_id, runtime.operation_id, runtime.thread_id, runtime.turn_id]
        ):
            raise CodexPlanGateError("Task has no Codex runtime to interrupt")
        async with self.adapter_factory() as adapter:
            remote = await adapter.interrupt(
                workflow_id=runtime.workflow_id,
                operation_id=runtime.operation_id,
                thread_id=runtime.thread_id,
                turn_id=runtime.turn_id,
            )
        current, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_INTERRUPTED,
            workflow_id=runtime.workflow_id,
            operation_id=runtime.operation_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            remote_status="interrupted",
            next_action=_string(remote, "nextRecommendedAction"),
            task_phase=TaskPhase.PAUSED,
            current_state=reason or "Codex turn interrupted by Supervisor.",
            event_payload={"reason": reason},
        )
        return {
            "task": current.model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "remote": remote,
        }

    async def pending_interactions(self, task_id: str) -> dict[str, Any]:
        self.memory.get_task(task_id)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            return {"task_id": task_id, "interactions": [], "runtime": None}
        async with self.adapter_factory() as adapter:
            remote = await adapter.list_pending_interactions(
                workflow_id=runtime.workflow_id,
                operation_id=runtime.operation_id,
                thread_id=runtime.thread_id,
                turn_id=runtime.turn_id,
            )
        return {
            "task_id": task_id,
            "runtime": runtime.model_dump(mode="json"),
            "remote": remote,
        }

    async def answer_interaction(
        self,
        task_id: str,
        expected_revision: int,
        interaction_id: str,
        *,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        self.memory.assert_revision(task_id, expected_revision)
        runtime = get_codex_runtime(self.memory.store, task_id)
        if runtime is None:
            raise CodexPlanGateError("Task has no Codex runtime")
        async with self.adapter_factory() as adapter:
            remote = await adapter.answer_pending_interaction(
                interaction_id,
                decision=decision,
                answers=answers,
                scope=scope,
            )
        current, current_runtime = bind_codex_runtime(
            self.memory.store,
            task_id,
            expected_revision,
            event_type=EventType.CODEX_PROGRESS,
            workflow_id=runtime.workflow_id,
            operation_id=runtime.operation_id,
            thread_id=runtime.thread_id,
            turn_id=runtime.turn_id,
            remote_status=runtime.remote_status,
            next_action=runtime.next_action,
            event_payload={"interaction_id": interaction_id, "answered": True},
        )
        return {
            "task": current.model_dump(mode="json"),
            "runtime": current_runtime.model_dump(mode="json"),
            "remote": remote,
        }
