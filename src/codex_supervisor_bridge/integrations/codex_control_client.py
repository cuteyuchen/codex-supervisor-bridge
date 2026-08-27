from __future__ import annotations

import json
import os
from typing import Any

from mcp import Client, StdioServerParameters

from .codex_control_errors import (
    CodexContractError,
    CodexControlUnavailableError,
    CodexToolError,
)

CONTRACT_VERSION = "1"
SERVER_NAME = "codex-control-plane-mcp"
REQUIRED_STABLE_TOOLS = {
    "codex_health_summary",
    "codex_get_runtime_capabilities",
    "codex_preflight_project_run",
    "codex_start_plan_workflow",
    "codex_get_workflow_status",
    "codex_approve_plan",
    "codex_submit_task",
    "codex_get_operation_status",
    "codex_list_pending_interactions",
    "codex_answer_pending_interaction",
    "codex_interrupt_turn",
}


class CodexControlAdapter:
    """Typed MCP client for ``codex-control-plane-mcp``.

    The bridge treats the upstream MCP as the sole Codex write/control path.
    It never mutates Codex SQLite, transcript files, or app-server state
    directly. A successful contract handshake is required before write calls.
    """

    def __init__(self, target: Any) -> None:
        self._client = Client(target)
        self._entered = False
        self._contract_verified = False
        self._health: dict[str, Any] | None = None

    @classmethod
    def stdio(
        cls,
        command: str = "codex-control-plane-mcp",
        *,
        args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> "CodexControlAdapter":
        merged_env = None
        if env is not None:
            merged_env = {**os.environ, **env}
        return cls(
            StdioServerParameters(
                command=command,
                args=args or [],
                env=merged_env,
            )
        )

    async def __aenter__(self) -> "CodexControlAdapter":
        try:
            await self._client.__aenter__()
        except Exception as exc:
            raise CodexControlUnavailableError() from exc
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            await self._client.__aexit__(exc_type, exc, tb)
        finally:
            self._entered = False
            self._contract_verified = False
            self._health = None

    def _require_connected(self) -> None:
        if not self._entered:
            raise RuntimeError("CodexControlAdapter must be used inside 'async with'")

    @staticmethod
    def _text(result: Any) -> str:
        return "\n".join(
            item.text
            for item in result.content
            if isinstance(getattr(item, "text", None), str)
        ).strip()

    @classmethod
    def _decode_payload(cls, tool: str, result: Any) -> dict[str, Any]:
        structured = result.structured_content
        payload: dict[str, Any] | None = structured if isinstance(structured, dict) else None
        if payload is None:
            text = cls._text(result)
            if text:
                try:
                    decoded = json.loads(text)
                except json.JSONDecodeError as exc:
                    raise CodexContractError(
                        f"{tool} returned non-JSON MCP content"
                    ) from exc
                if isinstance(decoded, dict):
                    payload = decoded
        payload = payload or {}

        if result.is_error or payload.get("ok") is False:
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            code = str(error.get("code") or "CODEX_TOOL_ERROR")
            message = str(error.get("message") or cls._text(result) or "Codex tool failed")
            retryable = bool(error.get("retryable", False))
            raise CodexToolError(tool, code, message, retryable=retryable)
        return payload

    async def _call_unchecked(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_connected()
        result = await self._client.call_tool(tool, arguments or {})
        return self._decode_payload(tool, result)

    async def verify_contract(self) -> dict[str, Any]:
        health = await self._call_unchecked("codex_health_summary")
        if health.get("ok") is not True:
            raise CodexContractError("health summary did not report ok=true")
        version = health.get("version")
        if not isinstance(version, dict):
            raise CodexContractError("health summary has no version contract")
        if version.get("serverName") != SERVER_NAME:
            raise CodexContractError(
                f"unexpected serverName {version.get('serverName')!r}; expected {SERVER_NAME!r}"
            )
        if str(version.get("contractVersion")) != CONTRACT_VERSION:
            raise CodexContractError(
                f"unsupported contractVersion {version.get('contractVersion')!r}; "
                f"expected {CONTRACT_VERSION!r}"
            )
        if not version.get("toolSurfaceHash"):
            raise CodexContractError("toolSurfaceHash is missing")
        if not version.get("guideHash"):
            raise CodexContractError("guideHash is missing")
        stable = version.get("stableTools")
        if not isinstance(stable, list):
            raise CodexContractError("version.stableTools is missing")
        missing = sorted(REQUIRED_STABLE_TOOLS - {str(item) for item in stable})
        if missing:
            raise CodexContractError(f"required stable tools are missing: {missing}")
        self._contract_verified = True
        self._health = health
        return health

    async def _call(
        self,
        tool: str,
        arguments: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._contract_verified:
            await self.verify_contract()
        return await self._call_unchecked(tool, arguments)

    async def health(self) -> dict[str, Any]:
        return await self.verify_contract()

    async def runtime_capabilities(self) -> dict[str, Any]:
        return await self._call("codex_get_runtime_capabilities")

    async def preflight(
        self,
        *,
        project_id: str | None = None,
        cwd: str | None = None,
        model: str | None = None,
        workflow_kind: str = "plan",
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"workflow_kind": workflow_kind, "live_probe": False}
        if project_id:
            args["project_id"] = project_id
        if cwd:
            args["cwd"] = cwd
        if model:
            args["model"] = model
        return await self._call("codex_preflight_project_run", args)

    async def start_plan_workflow(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._call("codex_start_plan_workflow", arguments)

    async def get_workflow_status(
        self,
        workflow_id: str,
        *,
        last_messages: int = 10,
    ) -> dict[str, Any]:
        return await self._call(
            "codex_get_workflow_status",
            {
                "workflow_id": workflow_id,
                "last_messages": last_messages,
                "include_events": True,
                "refresh_live": False,
                "refresh_live_goal": False,
            },
        )

    async def approve_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._call("codex_approve_plan", arguments)

    async def steer_turn(
        self,
        *,
        thread_id: str,
        expected_turn_id: str,
        message: str,
        client_request_id: str,
    ) -> dict[str, Any]:
        return await self._call(
            "codex_submit_task",
            {
                "operation_type": "steer_turn",
                "thread_id": thread_id,
                "expected_turn_id": expected_turn_id,
                "message": message,
                "client_request_id": client_request_id,
            },
        )

    async def get_operation_status(self, operation_id: str) -> dict[str, Any]:
        return await self._call(
            "codex_get_operation_status",
            {
                "operation_id": operation_id,
                "last_messages": 10,
                "progress_events": 20,
                "include_events": False,
            },
        )

    async def list_pending_interactions(
        self,
        *,
        workflow_id: str | None = None,
        operation_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"status": "pending", "limit": 50}
        if workflow_id:
            args["workflow_id"] = workflow_id
        if operation_id:
            args["operation_id"] = operation_id
        if thread_id:
            args["thread_id"] = thread_id
        if turn_id:
            args["turn_id"] = turn_id
        return await self._call("codex_list_pending_interactions", args)

    async def answer_pending_interaction(
        self,
        interaction_id: str,
        *,
        decision: str | None = None,
        answers: dict[str, Any] | None = None,
        scope: str = "turn",
    ) -> dict[str, Any]:
        args: dict[str, Any] = {"interaction_id": interaction_id, "scope": scope}
        if decision is not None:
            args["decision"] = decision
        if answers is not None:
            args["answers"] = answers
        return await self._call("codex_answer_pending_interaction", args)

    async def interrupt(
        self,
        *,
        workflow_id: str | None = None,
        operation_id: str | None = None,
        thread_id: str | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {}
        if workflow_id:
            args["workflow_id"] = workflow_id
        if operation_id:
            args["operation_id"] = operation_id
        if thread_id:
            args["thread_id"] = thread_id
        if turn_id:
            args["turn_id"] = turn_id
        return await self._call("codex_interrupt_turn", args)
