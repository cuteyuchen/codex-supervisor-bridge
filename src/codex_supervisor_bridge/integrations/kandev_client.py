from __future__ import annotations

import json
from types import TracebackType
from typing import Any

from mcp import Client

from .kandev_errors import (
    KandevCapabilityError,
    KandevError,
    KandevProtocolError,
    KandevToolError,
)
from .kandev_models import KandevCapabilities, KandevCreateTaskRequest


REQUIRED_EXTERNAL_TOOLS = {
    "list_workspaces_kandev",
    "list_workflows_kandev",
    "list_workflow_steps_kandev",
    "list_repositories_kandev",
    "list_tasks_kandev",
    "create_task_kandev",
    "list_task_sessions_kandev",
    "get_task_conversation_kandev",
    "move_task_kandev",
    "update_task_state_kandev",
}


class KandevAdapter:
    """Typed client for Kandev's external Streamable HTTP MCP endpoint.

    Production normally passes ``http://127.0.0.1:38429/mcp``. Tests may pass
    an in-process MCPServer object so the exact MCP protocol path is exercised
    without opening a local port.
    """

    def __init__(self, target: Any = "http://127.0.0.1:38429/mcp") -> None:
        self.target = target
        self._client = Client(target)
        self._entered = False
        self._tool_names: set[str] | None = None

    async def __aenter__(self) -> "KandevAdapter":
        try:
            await self._client.__aenter__()
        except Exception as exc:
            raise KandevError("Kandev external MCP is unavailable") from exc
        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._client.__aexit__(exc_type, exc, tb)
        finally:
            self._entered = False
            self._tool_names = None

    def _require_connected(self) -> None:
        if not self._entered:
            raise RuntimeError("KandevAdapter must be used inside 'async with'")

    async def capabilities(self) -> KandevCapabilities:
        self._require_connected()
        listed = await self._client.list_tools()
        tools = sorted(tool.name for tool in listed.tools)
        self._tool_names = set(tools)
        missing = sorted(REQUIRED_EXTERNAL_TOOLS - self._tool_names)
        return KandevCapabilities(tools=tools, missing_required_tools=missing)

    async def require_compatible(self) -> KandevCapabilities:
        capabilities = await self.capabilities()
        if not capabilities.compatible:
            raise KandevCapabilityError(capabilities.missing_required_tools)
        return capabilities

    async def _ensure_tool(self, tool: str) -> None:
        if self._tool_names is None:
            await self.capabilities()
        assert self._tool_names is not None
        if tool not in self._tool_names:
            raise KandevCapabilityError([tool])

    @staticmethod
    def _text_content(result: Any) -> str:
        return "\n".join(
            getattr(item, "text", "")
            for item in result.content
            if getattr(item, "text", None) is not None
        ).strip()

    @classmethod
    def _payload_from_result(cls, tool: str, result: Any) -> dict[str, Any]:
        if result.is_error:
            message = cls._text_content(result) or "unknown Kandev MCP error"
            raise KandevToolError(tool, message)

        structured = result.structured_content
        if isinstance(structured, dict) and structured:
            # MCP Python servers can provide structured_content. Kandev's Go
            # external server currently returns JSON TextContent, but accepting
            # both shapes keeps this adapter protocol-oriented rather than
            # coupled to one server implementation detail.
            return structured

        text = cls._text_content(result)
        if not text:
            return {}
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise KandevProtocolError(
                f"Kandev tool {tool} returned non-JSON text content"
            ) from exc
        if not isinstance(decoded, dict):
            raise KandevProtocolError(
                f"Kandev tool {tool} returned JSON {type(decoded).__name__}; expected object"
            )
        return decoded

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_connected()
        await self._ensure_tool(tool)
        result = await self._client.call_tool(tool, arguments or {})
        return self._payload_from_result(tool, result)

    async def list_workspaces(self) -> dict[str, Any]:
        return await self.call("list_workspaces_kandev")

    async def list_workflows(self, workspace_id: str) -> dict[str, Any]:
        return await self.call("list_workflows_kandev", {"workspace_id": workspace_id})

    async def list_workflow_steps(self, workflow_id: str) -> dict[str, Any]:
        return await self.call("list_workflow_steps_kandev", {"workflow_id": workflow_id})

    async def list_repositories(self, workspace_id: str) -> dict[str, Any]:
        return await self.call("list_repositories_kandev", {"workspace_id": workspace_id})

    async def list_tasks(self, workflow_id: str) -> dict[str, Any]:
        return await self.call("list_tasks_kandev", {"workflow_id": workflow_id})

    async def create_task(self, request: KandevCreateTaskRequest) -> dict[str, Any]:
        return await self.call("create_task_kandev", request.to_tool_arguments())

    async def list_task_sessions(self, task_id: str) -> dict[str, Any]:
        return await self.call("list_task_sessions_kandev", {"task_id": task_id})

    async def get_task_conversation(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {"task_id": task_id}
        if session_id:
            arguments["session_id"] = session_id
        if limit is not None:
            arguments["limit"] = limit
        return await self.call("get_task_conversation_kandev", arguments)

    async def move_task(
        self,
        task_id: str,
        workflow_id: str,
        workflow_step_id: str,
        *,
        position: int | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        arguments: dict[str, Any] = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "workflow_step_id": workflow_step_id,
        }
        if position is not None:
            arguments["position"] = position
        if prompt:
            arguments["prompt"] = prompt
        return await self.call("move_task_kandev", arguments)

    async def update_task_state(self, task_id: str, state: str) -> dict[str, Any]:
        return await self.call(
            "update_task_state_kandev",
            {"task_id": task_id, "state": state},
        )


def extract_kandev_task_id(payload: dict[str, Any]) -> str:
    """Extract a task ID from known Kandev response shapes without guessing text."""
    for key in ("task_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    task = payload.get("task")
    if isinstance(task, dict):
        for key in ("task_id", "id"):
            value = task.get(key)
            if isinstance(value, str) and value:
                return value
    raise KandevProtocolError("Kandev create_task response did not contain a task ID")
