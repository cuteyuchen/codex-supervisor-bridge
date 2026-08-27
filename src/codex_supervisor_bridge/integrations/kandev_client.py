from __future__ import annotations

import json
from typing import Any

from mcp import Client

from . import kandev_errors, kandev_models


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
    """Typed client for Kandev's external Streamable HTTP MCP endpoint."""

    def __init__(self, target: Any = "http://127.0.0.1:38429/mcp") -> None:
        self._client = Client(target)
        self._entered = False
        self._tool_names: set[str] | None = None

    async def __aenter__(self) -> "KandevAdapter":
        try:
            await self._client.__aenter__()
        except Exception as exc:
            raise kandev_errors.KandevError("Kandev external MCP is unavailable") from exc
        self._entered = True
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        try:
            await self._client.__aexit__(exc_type, exc, tb)
        finally:
            self._entered = False
            self._tool_names = None

    def _require_connected(self) -> None:
        if not self._entered:
            raise RuntimeError("KandevAdapter must be used inside 'async with'")

    async def capabilities(self) -> kandev_models.KandevCapabilities:
        self._require_connected()
        listed = await self._client.list_tools()
        tools = sorted(tool.name for tool in listed.tools)
        self._tool_names = set(tools)
        return kandev_models.KandevCapabilities(
            tools=tools,
            missing_required_tools=sorted(REQUIRED_EXTERNAL_TOOLS - self._tool_names),
        )

    async def require_compatible(self) -> kandev_models.KandevCapabilities:
        capabilities = await self.capabilities()
        if not capabilities.compatible:
            raise kandev_errors.KandevCapabilityError(capabilities.missing_required_tools)
        return capabilities

    async def _ensure_tool(self, tool: str) -> None:
        if self._tool_names is None:
            await self.capabilities()
        assert self._tool_names is not None
        if tool not in self._tool_names:
            raise kandev_errors.KandevCapabilityError([tool])

    @staticmethod
    def _text(result: Any) -> str:
        return "\n".join(
            item.text
            for item in result.content
            if isinstance(getattr(item, "text", None), str)
        ).strip()

    @classmethod
    def _payload(cls, tool: str, result: Any) -> dict[str, Any]:
        if result.is_error:
            raise kandev_errors.KandevToolError(
                tool,
                cls._text(result) or "unknown Kandev MCP error",
            )
        structured = result.structured_content
        if isinstance(structured, dict) and structured:
            if set(structured) == {"result"} and isinstance(structured["result"], str):
                try:
                    nested = json.loads(structured["result"])
                except json.JSONDecodeError:
                    return structured
                if isinstance(nested, dict):
                    return nested
            return structured
        text = cls._text(result)
        if not text:
            return {}
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise kandev_errors.KandevProtocolError(
                f"Kandev tool {tool} returned non-JSON text content"
            ) from exc
        if not isinstance(payload, dict):
            raise kandev_errors.KandevProtocolError(
                f"Kandev tool {tool} returned JSON {type(payload).__name__}; expected object"
            )
        return payload

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_connected()
        await self._ensure_tool(tool)
        result = await self._client.call_tool(tool, arguments or {})
        return self._payload(tool, result)

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

    async def create_task(self, request: kandev_models.KandevCreateTaskRequest) -> dict[str, Any]:
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
        args: dict[str, Any] = {"task_id": task_id}
        if session_id:
            args["session_id"] = session_id
        if limit is not None:
            args["limit"] = limit
        return await self.call("get_task_conversation_kandev", args)

    async def move_task(
        self,
        task_id: str,
        workflow_id: str,
        workflow_step_id: str,
        *,
        position: int | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        args: dict[str, Any] = {
            "task_id": task_id,
            "workflow_id": workflow_id,
            "workflow_step_id": workflow_step_id,
        }
        if position is not None:
            args["position"] = position
        if prompt:
            args["prompt"] = prompt
        return await self.call("move_task_kandev", args)

    async def update_task_state(self, task_id: str, state: str) -> dict[str, Any]:
        return await self.call("update_task_state_kandev", {"task_id": task_id, "state": state})


def extract_kandev_task_id(payload: dict[str, Any]) -> str:
    """Extract a task ID from known Kandev response shapes without text guessing."""
    for key in ("task_id", "id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    nested = payload.get("task")
    if isinstance(nested, dict):
        for key in ("task_id", "id"):
            value = nested.get(key)
            if isinstance(value, str) and value:
                return value
    raise kandev_errors.KandevProtocolError(
        "Kandev create_task response did not contain a task ID"
    )
