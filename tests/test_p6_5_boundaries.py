from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from mcp import Client

from codex_supervisor_bridge.integrations.devspace_errors import DevSpaceToolError
from codex_supervisor_bridge.mcp.errors import expose_integration_errors
from codex_supervisor_bridge.mcp.server import create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def text_content(result: Any) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "text", None) is not None
    )


def test_direct_semantic_tools_are_registered_but_raw_devspace_surface_is_hidden() -> None:
    service = MemoryService()

    async def scenario() -> None:
        async with Client(create_mcp_server(service)) as client:
            listed = await client.list_tools()
            names = {item.name for item in listed.tools}
            assert {
                "get_direct_workspace_status",
                "open_direct_workspace",
                "read_direct_workspace_file",
                "get_direct_git_state",
                "refresh_direct_git_state",
                "get_direct_workspace_changes",
                "apply_direct_workspace_patch",
            } <= names
            assert not {
                "exec_command",
                "apply_patch",
                "open_workspace",
                "write_stdin",
            } & names

    try:
        asyncio.run(scenario())
    finally:
        service.close()


def test_devspace_tool_error_is_redacted_at_mcp_boundary() -> None:
    @expose_integration_errors
    async def fail() -> None:
        raise DevSpaceToolError(
            "exec_command",
            "token=super-secret path=C:/Users/alice/private/project/db.sqlite",
        )

    async def scenario() -> None:
        try:
            await fail()
        except Exception as exc:
            message = str(exc)
            assert "Local workspace operation failed" in message
            assert "super-secret" not in message
            assert "db.sqlite" not in message
            assert "exec_command" not in message
        else:
            raise AssertionError("expected ToolError")

    asyncio.run(scenario())


def test_context_pack_keeps_workspace_review_and_reconciliation_state(tmp_path: Path) -> None:
    from codex_supervisor_bridge.memory.context_pack import ContextPackBuilder
    from codex_supervisor_bridge.memory.execution import acquire_writer
    from codex_supervisor_bridge.memory.models import ActiveWriter, Actor
    from codex_supervisor_bridge.memory.workspace import (
        bind_workspace,
        mark_direct_operation_reconciliation_required,
    )

    service = MemoryService(tmp_path / "context.db")
    try:
        task = service.store.create_task("CTX-P65", "Context", repository="C:/repo")
        task, _ = bind_workspace(
            service.store,
            task.task_id,
            task.revision,
            backend_name="devspace",
            workspace_id="ws-ctx",
            repository="C:/repo",
            root="C:/repo",
            workspace_mode="worktree",
            git_branch="main",
            git_head="a" * 40,
        )
        acquired = acquire_writer(
            service.store,
            task.task_id,
            task.revision,
            ActiveWriter.CHATGPT,
            actor=Actor.SUPERVISOR,
        )
        prepared = service.store
        from codex_supervisor_bridge.memory.workspace import prepare_direct_operation

        operation = prepare_direct_operation(
            prepared,
            task.task_id,
            acquired.task.revision,
            acquired.execution.writer_epoch,
            operation_type="APPLY_PATCH",
            request_digest="sha256:test",
        )
        mark_direct_operation_reconciliation_required(
            prepared,
            task.task_id,
            operation.operation.operation_id,
            summary="unknown",
        )
        pack = ContextPackBuilder(prepared).build(task.task_id)
        assert "WORKSPACE STATE" in pack.content
        assert "Latest Review Ref" in pack.content
        assert "RECONCILIATION REQUIRED" in pack.content
        assert "PREPARED DIRECT OPERATION" not in pack.content
        assert "LATEST SUPERVISOR CHECKPOINT" in pack.content
    finally:
        service.close()
