from __future__ import annotations

import asyncio
from typing import Any

from mcp.client.client import InMemoryTransport
from mcp.server import MCPServer

from codex_supervisor_bridge.backends.models import BackendHealthStatus, WriterLeaseToken
from codex_supervisor_bridge.integrations.devspace_client import DevSpaceWorkspaceAdapter
from codex_supervisor_bridge.memory.models import ActiveWriter


def fake_devspace(*, omit: str | None = None) -> tuple[MCPServer, dict[str, Any]]:
    server = MCPServer("fake-devspace")
    state: dict[str, Any] = {"calls": []}

    if omit != "open_workspace":
        @server.tool()
        def open_workspace(
            path: str,
            mode: str | None = None,
            baseRef: str | None = None,
        ) -> dict[str, Any]:
            state["calls"].append(("open_workspace", path, mode, baseRef))
            return {
                "workspaceId": "ws-direct-1",
                "root": "C:/worktrees/direct-1",
                "mode": mode or "checkout",
                "worktree": {
                    "path": "C:/worktrees/direct-1",
                    "baseRef": baseRef or "HEAD",
                    "baseSha": "a" * 40,
                    "dirtySource": False,
                    "detached": False,
                    "managed": True,
                } if mode == "worktree" else None,
                "review": {"available": True},
                "instruction": "Follow AGENTS.md",
            }

    if omit != "read":
        @server.tool()
        def read(
            workspaceId: str,
            path: str,
            offset: int | None = None,
            limit: int | None = None,
        ) -> dict[str, Any]:
            state["calls"].append(("read", workspaceId, path, offset, limit))
            return {"result": f"{path}:{offset}:{limit}"}

    if omit != "apply_patch":
        @server.tool()
        def apply_patch(workspaceId: str, patch: str) -> dict[str, Any]:
            state["calls"].append(("apply_patch", workspaceId, patch))
            return {
                "result": "Applied patch to 1 file(s): src/app.py",
                "additions": 2,
                "removals": 1,
                "files": [{"path": "src/app.py", "operation": "update"}],
            }

    if omit != "exec_command":
        @server.tool()
        def exec_command(
            workspaceId: str,
            cmd: str,
            tty: bool | None = None,
            yieldTimeMs: int | None = None,
            maxOutputTokens: int | None = None,
        ) -> dict[str, Any]:
            state["calls"].append(
                ("exec_command", workspaceId, cmd, tty, yieldTimeMs, maxOutputTokens)
            )
            if cmd.startswith("git status"):
                return {
                    "result": (
                        "## main...origin/main\n"
                        " M src/app.py\n"
                        + ("b" * 40)
                        + "\nProcess exited with code 0."
                    ),
                    "running": False,
                    "exitCode": 0,
                    "wallTimeMs": 4,
                    "outputTruncated": False,
                }
            if cmd == "long-task":
                return {
                    "result": "started\nProcess running with session ID 17.",
                    "sessionId": 17,
                    "running": True,
                    "wallTimeMs": 10_000,
                    "outputTruncated": False,
                }
            return {
                "result": "ok\nProcess exited with code 0.",
                "running": False,
                "exitCode": 0,
                "wallTimeMs": 2,
                "outputTruncated": False,
            }

    if omit != "write_stdin":
        @server.tool()
        def write_stdin(
            workspaceId: str,
            sessionId: int,
            chars: str | None = None,
            yieldTimeMs: int | None = None,
            maxOutputTokens: int | None = None,
        ) -> dict[str, Any]:
            state["calls"].append(
                ("write_stdin", workspaceId, sessionId, chars, yieldTimeMs, maxOutputTokens)
            )
            return {
                "result": "done\nProcess exited with code 0.",
                "sessionId": sessionId,
                "running": False,
                "exitCode": 0,
                "wallTimeMs": 12_000,
                "outputTruncated": False,
            }

    if omit != "show_changes":
        @server.tool()
        def show_changes(workspaceId: str) -> dict[str, Any]:
            state["calls"].append(("show_changes", workspaceId))
            return {
                "workspaceId": workspaceId,
                "reviewRef": "c" * 40,
                "result": "1 file changed",
            }

    return server, state


def lease() -> WriterLeaseToken:
    return WriterLeaseToken(
        task_id="DIRECT-1",
        writer=ActiveWriter.CHATGPT,
        writer_epoch=3,
        task_revision=9,
    )


def test_devspace_adapter_maps_workspace_read_patch_command_review_and_git() -> None:
    upstream, state = fake_devspace()

    async def scenario() -> None:
        async with DevSpaceWorkspaceAdapter(upstream) as adapter:
            health = await adapter.health()
            assert health.status == BackendHealthStatus.READY

            workspace = await adapter.open_workspace(
                "C:/src/project",
                worktree=True,
                base_ref="main",
            )
            assert workspace.workspace_id == "ws-direct-1"
            assert workspace.worktree is True
            assert workspace.git.head == "a" * 40

            text = await adapter.read(
                workspace.workspace_id,
                "src/app.py",
                start_line=10,
                end_line=14,
            )
            assert text == "src/app.py:10:5"

            patched = await adapter.apply_patch(
                workspace.workspace_id,
                "*** Begin Patch\n*** End Patch",
                lease=lease(),
            )
            assert patched.files == ["src/app.py"]

            running = await adapter.run_command(
                workspace.workspace_id,
                "long-task",
                lease=lease(),
            )
            assert running.status == "running"
            assert running.command_id == "17"

            completed = await adapter.poll_command(workspace.workspace_id, "17")
            assert completed.status == "completed"
            assert completed.exit_code == 0

            review = await adapter.show_changes(workspace.workspace_id)
            assert review.review_ref == "c" * 40
            assert review.summary == "1 file changed"

            git = await adapter.git_state(workspace.workspace_id)
            assert git.branch == "main"
            assert git.head == "b" * 40
            assert git.dirty is True
            assert git.changed_files == ["src/app.py"]

        assert any(call[0] == "open_workspace" and call[2] == "worktree" for call in state["calls"])
        assert any(call[0] == "exec_command" and call[2].startswith("git status") for call in state["calls"])

    asyncio.run(scenario())


def test_devspace_adapter_rejects_non_chatgpt_writer_for_direct_mutation() -> None:
    upstream, _ = fake_devspace()
    bad_lease = WriterLeaseToken(
        task_id="DIRECT-1",
        writer=ActiveWriter.CODEX,
        writer_epoch=2,
        task_revision=8,
    )

    async def scenario() -> None:
        async with DevSpaceWorkspaceAdapter(upstream) as adapter:
            try:
                await adapter.apply_patch(
                    "ws-direct-1",
                    "*** Begin Patch\n*** End Patch",
                    lease=bad_lease,
                )
            except ValueError as exc:
                assert "CHATGPT writer lease" in str(exc)
            else:
                raise AssertionError("expected direct mutation to reject CODEX lease")

    asyncio.run(scenario())


def test_devspace_adapter_health_degrades_when_required_tool_is_missing() -> None:
    upstream, _ = fake_devspace(omit="write_stdin")

    async def scenario() -> None:
        async with DevSpaceWorkspaceAdapter(upstream) as adapter:
            health = await adapter.health()
            assert health.status == BackendHealthStatus.DEGRADED
            assert health.repairable is True
            assert health.technical_detail is not None
            assert "write_stdin" in health.technical_detail

    asyncio.run(scenario())


def test_devspace_adapter_accepts_authenticated_transport_factory() -> None:
    upstream, state = fake_devspace()

    def authenticated_transport() -> InMemoryTransport:
        return InMemoryTransport(upstream)

    async def scenario() -> None:
        async with DevSpaceWorkspaceAdapter(
            "http://127.0.0.1:39101/mcp",
            transport_factory=authenticated_transport,
        ) as adapter:
            health = await adapter.health()
            assert health.status == BackendHealthStatus.READY

            workspace = await adapter.open_workspace(
                "C:/src/project",
                worktree=True,
                base_ref="main",
            )
            assert workspace.workspace_id == "ws-direct-1"

        assert any(call[0] == "open_workspace" for call in state["calls"])

    asyncio.run(scenario())
