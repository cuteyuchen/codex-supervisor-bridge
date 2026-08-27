from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

from mcp import Client, StdioServerParameters

from codex_supervisor_bridge.backends.models import (
    AgentSnapshot,
    BackendHealth,
    BackendHealthStatus,
    PendingInteraction,
    PlanHandle,
    PlanResult,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.memory.models import ActiveWriter

from .codex_control_client import CodexControlAdapter
from .codex_control_errors import CodexControlError
from .local_codex_bridge_errors import (
    LocalCodexBridgeCapabilityError,
    LocalCodexBridgeError,
    LocalCodexBridgeProtocolError,
    LocalCodexBridgeToolError,
    LocalCodexBridgeUnavailableError,
    LocalCodexBridgeUnknownOutcomeError,
)

LocalClientFactory = Callable[[], Client]
ControlAdapterFactory = Callable[[], AbstractAsyncContextManager[CodexControlAdapter]]

LOCAL_CODEX_TOOLS = {
    "codex_turn",
    "codex_observe",
    "codex_steer",
    "codex_respond",
    "codex_interrupt",
}


def _value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _string(payload: dict[str, Any], *keys: str) -> str | None:
    value = _value(payload, *keys)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _strings(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip() and item.strip() not in result:
            result.append(item.strip()[:300])
        if len(result) >= limit:
            break
    return result


def _bounded_dict(value: Any, *, limit: int = 24) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:limit]:
        if isinstance(key, str) and isinstance(item, (str, int, float, bool)):
            result[key[:80]] = str(item)[:300] if isinstance(item, str) else item
    return result


def _decode_result(tool: str, result: Any) -> dict[str, Any]:
    structured = getattr(result, "structured_content", None)
    payload: dict[str, Any] | None = structured if isinstance(structured, dict) else None
    if payload and set(payload) == {"result"} and isinstance(payload.get("result"), str):
        try:
            nested = json.loads(payload["result"])
        except json.JSONDecodeError:
            nested = None
        if isinstance(nested, dict):
            payload = nested
    if payload is None:
        text = "\n".join(
            item.text
            for item in getattr(result, "content", [])
            if isinstance(getattr(item, "text", None), str)
        ).strip()
        if text:
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LocalCodexBridgeProtocolError(
                    "Local Codex Bridge returned non-JSON content"
                ) from exc
            payload = decoded if isinstance(decoded, dict) else None
    payload = payload or {}
    if getattr(result, "is_error", False) or payload.get("ok") is False:
        raise LocalCodexBridgeToolError(tool, "upstream tool reported an error")
    return payload


def _pending(raw: Any, *, thread_id: str | None, turn_id: str | None) -> list[PendingInteraction]:
    if not isinstance(raw, list):
        return []
    result: list[PendingInteraction] = []
    for item in raw[:50]:
        if not isinstance(item, dict):
            continue
        request_id = _value(item, "request_id", "requestId", "interaction_id", "interactionId", "id")
        if not isinstance(request_id, (str, int)):
            continue
        method = _string(item, "method", "kind", "type") or "unknown"
        lower = method.lower()
        if "command" in lower or "exec" in lower:
            kind = "command_approval"
        elif "file" in lower or "patch" in lower:
            kind = "file_change_approval"
        elif "permission" in lower:
            kind = "permissions_approval"
        elif "userinput" in lower or "user_input" in lower:
            kind = "user_input"
        else:
            kind = "provider_request"
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        summary = _string(item, "summary", "prompt", "description") or _string(
            params, "summary", "prompt", "description"
        )
        options = _strings(_value(item, "options") or _value(params, "options"), limit=12)
        runtime = {
            "request_id": request_id,
            "method": method,
            "thread_id": thread_id,
            "turn_id": turn_id,
        }
        result.append(
            PendingInteraction(
                interaction_id=str(request_id),
                kind=kind,
                type=kind,
                summary=summary,
                options=options,
                runtime_reference=runtime,
                prompt=summary,
                thread_id=thread_id,
                turn_id=turn_id,
                metadata={"request_id": request_id, "method": method},
            )
        )
    return result


def _plan_result(value: Any) -> PlanResult | None:
    if isinstance(value, str) and value.strip():
        return PlanResult(content=value.strip())
    if not isinstance(value, dict):
        return None
    content = _string(value, "content", "markdown", "text", "plan")
    if not content:
        return None
    return PlanResult(
        content=content,
        status=_string(value, "status", "quality") or "ready",
        plan_hash=_string(value, "plan_hash", "planHash", "hash"),
    )


def _plan_from_source(source: dict[str, Any]) -> PlanResult | None:
    plan = _plan_result(_value(source, "plan", "plan_result", "latest_plan", "latestPlan"))
    if plan is not None:
        return plan
    content = _string(source, "plan_content", "planContent")
    if not content:
        return None
    return PlanResult(
        content=content,
        status=_string(source, "plan_status", "planStatus") or "ready",
        plan_hash=_string(source, "plan_hash", "planHash"),
    )


def snapshot_from_payload(
    payload: dict[str, Any],
    *,
    operation_id: str | None = None,
    workflow_id: str | None = None,
    thread_id: str | None = None,
    turn_id: str | None = None,
    default_status: str = "unknown",
) -> AgentSnapshot:
    nested_payload = payload.get("remote") if isinstance(payload.get("remote"), dict) else None
    source = {**nested_payload, **payload} if nested_payload else payload
    status = _string(payload, "status", "runtime_status", "turn_status") or default_status
    nested = source.get("runtime") if isinstance(source.get("runtime"), dict) else {}
    if nested:
        status = _string(nested, "status", "runtime_status", "turn_status") or status
        thread_id = thread_id or _string(nested, "thread_id", "threadId", "active_thread_id")
        turn_id = turn_id or _string(nested, "turn_id", "turnId", "active_turn_id")
    status = _string(source, "status", "runtime_status", "turn_status") or status
    events = _value(source, "events", "progressEvents", "progress_events")
    completed = _strings(_value(source, "completed", "completed_items"))
    in_progress = _strings(_value(source, "in_progress", "inProgress", "active_items"))
    files = _strings(_value(source, "files_changed", "filesChanged", "changed_files"))
    validation = _bounded_dict(_value(source, "validation", "checks"))
    assumptions = _strings(_value(source, "assumptions"))
    deviations = _strings(_value(source, "deviations"))
    blockers = _strings(_value(source, "blockers", "errors"))
    risks = _strings(_value(source, "risks", "warnings"))
    next_steps = _strings(_value(source, "next_steps", "nextSteps"))
    pending = _pending(
        _value(
            source,
            "pending_requests",
            "pendingRequests",
            "pending_interactions",
            "pendingInteractions",
            "interactions",
        ),
        thread_id=thread_id,
        turn_id=turn_id,
    )
    plan = _plan_from_source(source)
    if pending:
        blockers.append(f"{len(pending)} pending Codex interaction(s)")
    if isinstance(events, list):
        for event in events[-20:]:
            if isinstance(event, dict):
                text = _string(event, "summary", "message", "text", "description")
                if text and text not in in_progress and len(in_progress) < 20:
                    in_progress.append(text[:300])
    return AgentSnapshot(
        status=status,
        plan=plan,
        operation_id=operation_id or _string(source, "operation_id", "operationId"),
        workflow_id=workflow_id or _string(source, "workflow_id", "workflowId"),
        thread_id=thread_id or _string(source, "thread_id", "threadId"),
        turn_id=turn_id or _string(source, "turn_id", "turnId"),
        completed=completed,
        in_progress=in_progress,
        files_changed=files,
        validation=validation,
        assumptions=assumptions,
        deviations=deviations,
        blockers=blockers,
        risks=risks,
        next_steps=next_steps,
        pending_interactions=pending,
        evidence_refs=_strings(_value(source, "evidence_refs", "evidenceRefs")),
        raw_event_count=len(events) if isinstance(events, list) else 0,
    )


def _handle(payload: dict[str, Any], *, default_status: str) -> PlanHandle:
    workflow = payload.get("workflow") if isinstance(payload.get("workflow"), dict) else {}
    operation = payload.get("operation") if isinstance(payload.get("operation"), dict) else {}
    turn = payload.get("turn") if isinstance(payload.get("turn"), dict) else {}
    source = {**workflow, **operation, **turn, **payload}
    plan = _plan_from_source(source)
    return PlanHandle(
        operation_id=_string(source, "operation_id", "operationId", "executionOperationId"),
        workflow_id=_string(source, "workflow_id", "workflowId"),
        thread_id=_string(source, "thread_id", "threadId"),
        turn_id=_string(source, "turn_id", "turnId", "executionTurnId", "planTurnId"),
        status=_string(source, "status", "phase") or default_status,
        plan=plan,
    )


def _unknown_handle(operation: str) -> PlanHandle:
    return PlanHandle(
        status="UNKNOWN",
        reconciliation_required=True,
        message=f"{operation} acknowledgement timed out; observe before retrying",
    )


def _unknown_snapshot(operation: str, *, handle: PlanHandle | None = None) -> AgentSnapshot:
    return AgentSnapshot(
        status="UNKNOWN",
        operation_id=handle.operation_id if handle else None,
        workflow_id=handle.workflow_id if handle else None,
        thread_id=handle.thread_id if handle else None,
        turn_id=handle.turn_id if handle else None,
        blockers=[f"{operation} acknowledgement timed out; outcome may have been accepted"],
        next_steps=["Observe runtime state before any retry"],
    )


def _is_timeout(exc: BaseException) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError)) or "timeout" in type(exc).__name__.lower()


class LocalCodexBridgeAgentBackend:
    """AgentBackend adapter for Local-Codex-Bridge's semantic MCP surface."""

    def __init__(self, target: Any, *, client_factory: LocalClientFactory | None = None) -> None:
        self.target = target
        self._client_factory = client_factory
        self._client: Client | None = None

    @classmethod
    def stdio(
        cls,
        command: str = "node",
        *,
        args: list[str],
        env: dict[str, str] | None = None,
    ) -> "LocalCodexBridgeAgentBackend":
        return cls(StdioServerParameters(command=command, args=args, env=env))

    async def __aenter__(self) -> "LocalCodexBridgeAgentBackend":
        if self._client is None:
            self._client = self._client_factory() if self._client_factory else Client(self.target)
            try:
                await self._client.__aenter__()
            except Exception as exc:
                self._client = None
                raise LocalCodexBridgeUnavailableError() from exc
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        if self._client is not None:
            try:
                await self._client.__aexit__(exc_type, exc, tb)
            finally:
                self._client = None

    async def _call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            return await self._call_client(self._client, tool, arguments)
        client = self._client_factory() if self._client_factory else Client(self.target)
        try:
            await client.__aenter__()
            return await self._call_client(client, tool, arguments)
        except LocalCodexBridgeError:
            raise
        except Exception as exc:
            raise LocalCodexBridgeUnavailableError() from exc
        finally:
            await client.__aexit__(None, None, None)

    @staticmethod
    async def _call_client(client: Client, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            result = await client.call_tool(tool, arguments)
        except (asyncio.TimeoutError, TimeoutError) as exc:
            raise LocalCodexBridgeUnknownOutcomeError(tool) from exc
        except Exception as exc:
            raise LocalCodexBridgeUnavailableError() from exc
        return _decode_result(tool, result)

    async def health(self) -> BackendHealth:
        try:
            if self._client is not None:
                listed = await self._client.list_tools()
            else:
                client = self._client_factory() if self._client_factory else Client(self.target)
                await client.__aenter__()
                try:
                    listed = await client.list_tools()
                finally:
                    await client.__aexit__(None, None, None)
            names = {tool.name for tool in listed.tools}
            missing = sorted(LOCAL_CODEX_TOOLS - names)
            if missing:
                raise LocalCodexBridgeCapabilityError(missing)
        except LocalCodexBridgeCapabilityError:
            return BackendHealth(
                capability="local_codex_bridge",
                status=BackendHealthStatus.DEGRADED,
                user_message="Codex is installed but needs repair.",
                repairable=True,
                technical_detail="required semantic tools are missing",
            )
        except LocalCodexBridgeError:
            return BackendHealth(
                capability="local_codex_bridge",
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex is not ready.",
                repairable=True,
                technical_detail="Local-Codex-Bridge could not be reached",
            )
        return BackendHealth(
            capability="local_codex_bridge",
            status=BackendHealthStatus.READY,
            user_message="Codex is ready.",
            technical_detail="semantic control surface verified",
        )

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        args = {
            "text": f"Plan task {task_id} in read-only mode.\n\n{context_pack}",
            "sandbox": "read-only",
            "approval_policy": "on-request",
        }
        if workspace.root:
            args["cwd"] = workspace.root
        try:
            return _handle(await self._call("codex_turn", args), default_status="planning")
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_handle("codex_turn")

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        if lease.writer != ActiveWriter.CODEX or lease.task_id != task_id:
            raise ValueError("Codex execution requires a current CODEX writer lease")
        args = {
            "text": (
                f"Implement the approved plan for task {task_id}.\n\n"
                f"Approved plan:\n{approved_plan}\n\nSupervisor context:\n{context_pack}"
            ),
            "sandbox": "workspace-write",
            "approval_policy": "on-request",
        }
        if workspace.root:
            args["cwd"] = workspace.root
        try:
            return _handle(await self._call("codex_turn", args), default_status="executing")
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_handle("codex_turn")

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        if not handle.thread_id:
            return _unknown_snapshot("observe", handle=handle)
        args: dict[str, Any] = {
            "thread_id": handle.thread_id,
            "limit": 50,
            "wait_ms": max(0, min(wait_ms, 30_000)),
        }
        if cursor is not None:
            args["cursor"] = max(0, cursor)
        try:
            payload = await self._call("codex_observe", args)
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_snapshot("observe", handle=handle)
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status=handle.status,
        )

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        if lease.writer != ActiveWriter.CODEX:
            raise ValueError("Codex steering requires a current CODEX writer lease")
        if not handle.thread_id or not handle.turn_id:
            return _unknown_snapshot("steer", handle=handle)
        try:
            payload = await self._call(
                "codex_steer",
                {
                    "thread_id": handle.thread_id,
                    "expected_turn_id": handle.turn_id,
                    "text": instruction,
                },
            )
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_snapshot("steer", handle=handle)
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="inProgress",
        )

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        if not handle.thread_id or not handle.turn_id:
            return _unknown_snapshot("interrupt", handle=handle)
        try:
            payload = await self._call(
                "codex_interrupt",
                {"thread_id": handle.thread_id, "turn_id": handle.turn_id},
            )
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_snapshot("interrupt", handle=handle)
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="interrupted",
        )

    async def list_pending_interactions(self, handle: PlanHandle) -> list[PendingInteraction]:
        return (await self.observe(handle)).pending_interactions

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot:
        runtime = interaction.runtime_reference or interaction.metadata
        request_id = runtime.get("request_id", interaction.interaction_id)
        method = runtime.get("method") or "item/tool/requestUserInput"
        args: dict[str, Any] = {
            "request_id": request_id,
            "thread_id": interaction.thread_id or handle.thread_id,
            "method": method,
        }
        if interaction.turn_id or handle.turn_id:
            args["turn_id"] = interaction.turn_id or handle.turn_id
        args.update(response)
        try:
            payload = await self._call("codex_respond", args)
        except LocalCodexBridgeUnknownOutcomeError:
            return _unknown_snapshot("respond", handle=handle)
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="inProgress",
        )

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)


class ControlPlaneAgentBackend:
    """Backend-neutral AgentBackend facade over the existing Control Plane adapter."""

    def __init__(self, adapter_factory: ControlAdapterFactory) -> None:
        self.adapter_factory = adapter_factory

    async def health(self) -> BackendHealth:
        try:
            async with self.adapter_factory() as adapter:
                await adapter.health()
        except CodexControlError:
            return BackendHealth(
                capability="control_plane",
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Codex is not ready.",
                repairable=True,
                technical_detail="Control Plane contract verification failed",
            )
        return BackendHealth(
            capability="control_plane",
            status=BackendHealthStatus.READY,
            user_message="Codex is ready.",
            technical_detail="Control Plane contract verified",
        )

    async def start_plan(
        self,
        *,
        task_id: str,
        context_pack: str,
        workspace: WorkspaceState,
    ) -> PlanHandle:
        args: dict[str, Any] = {
            "project_id": workspace.workspace_id,
            "message": f"Plan task {task_id} in read-only mode.\n\n{context_pack}",
            "sandbox": "read-only",
            "approval_policy": "on-request",
        }
        if workspace.root:
            args["cwd"] = workspace.root
        try:
            async with self.adapter_factory() as adapter:
                return _handle(await adapter.start_plan_workflow(args), default_status="planning")
        except Exception as exc:
            if _is_timeout(exc):
                return _unknown_handle("start_plan")
            raise

    async def get_plan_status(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)

    async def start_execution(
        self,
        *,
        task_id: str,
        context_pack: str,
        approved_plan: str,
        workspace: WorkspaceState,
        lease: WriterLeaseToken,
    ) -> PlanHandle:
        if lease.writer != ActiveWriter.CODEX or lease.task_id != task_id:
            raise ValueError("Codex execution requires a current CODEX writer lease")
        if not workspace.workspace_id:
            raise ValueError("workspace identity is required")
        try:
            async with self.adapter_factory() as adapter:
                payload = await adapter.approve_plan(
                    {
                        "workflow_id": workspace.workspace_id,
                        "message": f"Implement approved plan:\n{approved_plan}\n\n{context_pack}",
                        "sandbox": "workspace-write",
                        "approval_policy": "on-request",
                    }
                )
        except Exception as exc:
            if _is_timeout(exc):
                return _unknown_handle("start_execution")
            raise
        return _handle(payload, default_status="executing")

    async def observe(
        self,
        handle: PlanHandle,
        *,
        cursor: int | None = None,
        wait_ms: int = 0,
    ) -> AgentSnapshot:
        del cursor, wait_ms
        async with self.adapter_factory() as adapter:
            if handle.operation_id:
                payload = await adapter.get_operation_status(handle.operation_id)
            elif handle.workflow_id:
                payload = await adapter.get_workflow_status(handle.workflow_id)
            else:
                payload = {}
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status=handle.status,
        )

    async def steer(
        self,
        handle: PlanHandle,
        instruction: str,
        *,
        lease: WriterLeaseToken,
    ) -> AgentSnapshot:
        if lease.writer != ActiveWriter.CODEX:
            raise ValueError("Codex steering requires a current CODEX writer lease")
        if not handle.thread_id or not handle.turn_id:
            return _unknown_snapshot("steer", handle=handle)
        try:
            async with self.adapter_factory() as adapter:
                payload = await adapter.steer_turn(
                    thread_id=handle.thread_id,
                    expected_turn_id=handle.turn_id,
                    message=instruction,
                    client_request_id=f"supervisor-steer:{handle.thread_id}:{handle.turn_id}",
                )
        except Exception as exc:
            if _is_timeout(exc):
                return _unknown_snapshot("steer", handle=handle)
            raise
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="inProgress",
        )

    async def interrupt(self, handle: PlanHandle) -> AgentSnapshot:
        try:
            async with self.adapter_factory() as adapter:
                payload = await adapter.interrupt(
                    workflow_id=handle.workflow_id,
                    operation_id=handle.operation_id,
                    thread_id=handle.thread_id,
                    turn_id=handle.turn_id,
                )
        except Exception as exc:
            if _is_timeout(exc):
                return _unknown_snapshot("interrupt", handle=handle)
            raise
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="interrupted",
        )

    async def list_pending_interactions(self, handle: PlanHandle) -> list[PendingInteraction]:
        async with self.adapter_factory() as adapter:
            payload = await adapter.list_pending_interactions(
                workflow_id=handle.workflow_id,
                operation_id=handle.operation_id,
                thread_id=handle.thread_id,
                turn_id=handle.turn_id,
            )
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
        ).pending_interactions

    async def respond_interaction(
        self,
        handle: PlanHandle,
        interaction: PendingInteraction,
        response: dict[str, Any],
    ) -> AgentSnapshot:
        runtime = interaction.runtime_reference or interaction.metadata
        try:
            async with self.adapter_factory() as adapter:
                payload = await adapter.answer_pending_interaction(
                    str(runtime.get("request_id", interaction.interaction_id)),
                    decision=response.get("decision"),
                    answers=response.get("answers"),
                    scope=str(response.get("scope", "turn")),
                )
        except Exception as exc:
            if _is_timeout(exc):
                return _unknown_snapshot("respond", handle=handle)
            raise
        return snapshot_from_payload(
            payload,
            operation_id=handle.operation_id,
            workflow_id=handle.workflow_id,
            thread_id=handle.thread_id,
            turn_id=handle.turn_id,
            default_status="inProgress",
        )

    async def resume(self, handle: PlanHandle) -> AgentSnapshot:
        return await self.observe(handle)
