from __future__ import annotations

import re
from typing import Any

from mcp import Client

from codex_supervisor_bridge.backends.models import (
    BackendHealth,
    BackendHealthStatus,
    ChangeReview,
    CommandResult,
    GitState,
    WorkspaceState,
    WriterLeaseToken,
)
from codex_supervisor_bridge.integrations import devspace_errors
from codex_supervisor_bridge.memory.models import ActiveWriter

REQUIRED_DEVSPACE_TOOLS = {
    "open_workspace",
    "read",
    "apply_patch",
    "exec_command",
    "write_stdin",
    "show_changes",
}

_BRANCH_LINE = re.compile(r"^##\s+([^\.\s]+)(?:\.\.\.|\s|$)")
_PROCESS_STATUS_PREFIXES = (
    "Process exited with code ",
    "Process exited with signal ",
    "Process running with session ID ",
)


class DevSpaceWorkspaceAdapter:
    """WorkspaceBackend adapter for DevSpace's bounded coding MCP surface.

    This class intentionally models the upstream MCP protocol only. Supervisor
    revision/write-lease validation belongs in the direct-workspace coordinator
    so the adapter can remain replaceable and independently protocol-tested.
    """

    def __init__(self, target: Any = "http://127.0.0.1:7676/mcp") -> None:
        self._client = Client(target)
        self._entered = False
        self._tool_names: set[str] | None = None

    async def __aenter__(self) -> "DevSpaceWorkspaceAdapter":
        try:
            await self._client.__aenter__()
        except Exception as exc:
            raise devspace_errors.DevSpaceUnavailableError(
                "Local workspace service is unavailable"
            ) from exc
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
            raise RuntimeError("DevSpaceWorkspaceAdapter must be used inside 'async with'")

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
            raise devspace_errors.DevSpaceToolError(
                tool,
                cls._text(result) or "unknown local workspace error",
            )
        structured = result.structured_content
        if isinstance(structured, dict):
            return structured
        raise devspace_errors.DevSpaceProtocolError(
            f"Local workspace tool {tool} did not return structured content"
        )

    async def capabilities(self) -> set[str]:
        self._require_connected()
        listed = await self._client.list_tools()
        self._tool_names = {tool.name for tool in listed.tools}
        return set(self._tool_names)

    async def require_compatible(self) -> set[str]:
        tools = await self.capabilities()
        missing = sorted(REQUIRED_DEVSPACE_TOOLS - tools)
        if missing:
            raise devspace_errors.DevSpaceCapabilityError(missing)
        return tools

    async def _ensure_tool(self, tool: str) -> None:
        if self._tool_names is None:
            await self.capabilities()
        assert self._tool_names is not None
        if tool not in self._tool_names:
            raise devspace_errors.DevSpaceCapabilityError([tool])

    async def call(self, tool: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._require_connected()
        await self._ensure_tool(tool)
        try:
            result = await self._client.call_tool(tool, arguments or {})
        except devspace_errors.DevSpaceError:
            raise
        except Exception as exc:
            raise devspace_errors.DevSpaceUnavailableError(
                f"Local workspace request failed ({tool})"
            ) from exc
        return self._payload(tool, result)

    async def health(self) -> BackendHealth:
        try:
            tools = await self.require_compatible()
        except devspace_errors.DevSpaceCapabilityError as exc:
            return BackendHealth(
                capability="devspace",
                status=BackendHealthStatus.DEGRADED,
                user_message="Local workspace is installed but needs an update or repair.",
                repairable=True,
                technical_detail="missing tools: " + ", ".join(exc.missing_tools),
            )
        except devspace_errors.DevSpaceError as exc:
            return BackendHealth(
                capability="devspace",
                status=BackendHealthStatus.UNAVAILABLE,
                user_message="Local workspace is not ready.",
                repairable=True,
                technical_detail=str(exc),
            )
        return BackendHealth(
            capability="devspace",
            status=BackendHealthStatus.READY,
            user_message="Local workspace is ready.",
            technical_detail=f"tool_count={len(tools)}",
        )

    async def open_workspace(
        self,
        repository: str,
        *,
        worktree: bool = True,
        base_ref: str | None = None,
    ) -> WorkspaceState:
        args: dict[str, Any] = {
            "path": repository,
            "mode": "worktree" if worktree else "checkout",
        }
        if base_ref:
            args["baseRef"] = base_ref
        payload = await self.call("open_workspace", args)
        workspace_id = payload.get("workspaceId")
        root = payload.get("root")
        mode = payload.get("mode")
        if not isinstance(workspace_id, str) or not workspace_id:
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace open response did not contain workspaceId"
            )
        if not isinstance(root, str) or not root:
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace open response did not contain root"
            )
        if mode not in {"checkout", "worktree"}:
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace open response contained an invalid mode"
            )
        worktree_info = payload.get("worktree")
        base_sha = worktree_info.get("baseSha") if isinstance(worktree_info, dict) else None
        return WorkspaceState(
            workspace_id=workspace_id,
            repository=repository,
            root=root,
            worktree=mode == "worktree",
            git=GitState(head=base_sha if isinstance(base_sha, str) else None),
        )

    async def read(
        self,
        workspace_id: str,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        if start_line is not None and start_line < 1:
            raise ValueError("start_line must be >= 1")
        if end_line is not None and start_line is None:
            raise ValueError("end_line requires start_line")
        if end_line is not None and end_line < start_line:
            raise ValueError("end_line must be >= start_line")
        args: dict[str, Any] = {"workspaceId": workspace_id, "path": path}
        if start_line is not None:
            args["offset"] = start_line
        if end_line is not None:
            args["limit"] = end_line - start_line + 1
        payload = await self.call("read", args)
        result = payload.get("result")
        if not isinstance(result, str):
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace read response did not contain text result"
            )
        return result

    @staticmethod
    def _assert_chatgpt_lease(lease: WriterLeaseToken) -> None:
        if lease.writer != ActiveWriter.CHATGPT:
            raise ValueError("Direct workspace mutation requires a CHATGPT writer lease")

    async def apply_patch(
        self,
        workspace_id: str,
        patch: str,
        *,
        lease: WriterLeaseToken,
    ) -> ChangeReview:
        self._assert_chatgpt_lease(lease)
        payload = await self.call(
            "apply_patch",
            {"workspaceId": workspace_id, "patch": patch},
        )
        result = payload.get("result")
        files_payload = payload.get("files")
        files = [
            item["path"]
            for item in files_payload
            if isinstance(item, dict) and isinstance(item.get("path"), str)
        ] if isinstance(files_payload, list) else []
        if not isinstance(result, str):
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace patch response did not contain result"
            )
        return ChangeReview(summary=result, files=files)

    async def run_command(
        self,
        workspace_id: str,
        command: str,
        *,
        lease: WriterLeaseToken,
        yield_time_ms: int = 10_000,
        tty: bool = False,
    ) -> CommandResult:
        self._assert_chatgpt_lease(lease)
        payload = await self.call(
            "exec_command",
            {
                "workspaceId": workspace_id,
                "cmd": command,
                "tty": tty,
                "yieldTimeMs": max(0, min(yield_time_ms, 30_000)),
                "maxOutputTokens": 10_000,
            },
        )
        return self._command_result(payload)

    async def poll_command(
        self,
        workspace_id: str,
        command_id: str,
        *,
        input_text: str | None = None,
        interrupt: bool = False,
    ) -> CommandResult:
        try:
            session_id = int(command_id)
        except ValueError as exc:
            raise ValueError("DevSpace command_id must be a numeric process session id") from exc
        chars = input_text
        if interrupt:
            if chars:
                raise ValueError("input_text and interrupt cannot be used together")
            chars = "\u0003"
        args: dict[str, Any] = {
            "workspaceId": workspace_id,
            "sessionId": session_id,
            "yieldTimeMs": 10_000,
            "maxOutputTokens": 10_000,
        }
        if chars is not None:
            args["chars"] = chars
        payload = await self.call("write_stdin", args)
        return self._command_result(payload)

    @staticmethod
    def _command_result(payload: dict[str, Any]) -> CommandResult:
        result = payload.get("result")
        running = payload.get("running")
        session_id = payload.get("sessionId")
        exit_code = payload.get("exitCode")
        truncated = payload.get("outputTruncated", False)
        if not isinstance(result, str) or not isinstance(running, bool):
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace command response is missing result/running"
            )
        return CommandResult(
            command_id=str(session_id) if isinstance(session_id, int) else None,
            status="running" if running else "completed",
            exit_code=exit_code if isinstance(exit_code, int) else None,
            stdout=result,
            truncated=bool(truncated),
        )

    async def show_changes(self, workspace_id: str) -> ChangeReview:
        payload = await self.call("show_changes", {"workspaceId": workspace_id})
        result = payload.get("result")
        review_ref = payload.get("reviewRef")
        if not isinstance(result, str):
            raise devspace_errors.DevSpaceProtocolError(
                "Local workspace review response did not contain result"
            )
        return ChangeReview(
            review_ref=review_ref if isinstance(review_ref, str) else None,
            summary=result,
        )

    async def git_state(self, workspace_id: str) -> GitState:
        """Read Git state with a fixed internal command, not model-provided shell text."""
        payload = await self.call(
            "exec_command",
            {
                "workspaceId": workspace_id,
                "cmd": "git status --porcelain=v1 --branch && git rev-parse HEAD",
                "tty": False,
                "yieldTimeMs": 10_000,
                "maxOutputTokens": 2_000,
            },
        )
        command = self._command_result(payload)
        if command.status == "running" and command.command_id:
            command = await self.poll_command(workspace_id, command.command_id)
        if command.status == "running":
            raise devspace_errors.DevSpaceProtocolError(
                "Fixed Git state command did not complete within the bounded wait"
            )
        if command.exit_code not in (None, 0):
            raise devspace_errors.DevSpaceToolError("git_state", command.stdout)
        lines = [line for line in command.stdout.splitlines() if line.strip()]
        branch: str | None = None
        head: str | None = None
        changed: list[str] = []
        for line in lines:
            if line.startswith(_PROCESS_STATUS_PREFIXES):
                continue
            if line.startswith("## "):
                match = _BRANCH_LINE.match(line)
                branch = match.group(1) if match else line[3:].split()[0]
                continue
            if re.fullmatch(r"[0-9a-fA-F]{40,64}", line):
                head = line
                continue
            if len(line) >= 4 and line[:2].strip():
                changed.append(line[3:].strip())
        return GitState(
            branch=branch,
            head=head,
            dirty=bool(changed),
            changed_files=changed,
        )

    async def close_workspace(self, workspace_id: str) -> None:
        # Current DevSpace public MCP has no close-workspace tool. Workspaces are
        # process-managed; Supervisor records/relinquishes its binding locally.
        del workspace_id
