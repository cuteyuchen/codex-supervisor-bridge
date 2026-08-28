from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp import Client

from codex_supervisor_bridge.backends.models import BackendHealth, BackendHealthStatus
from codex_supervisor_bridge.mcp.server import _emit_readiness_marker, create_mcp_server
from codex_supervisor_bridge.memory.service import MemoryService


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def text_content(result: Any) -> str:
    return "\n".join(
        getattr(item, "text", "")
        for item in result.content
        if getattr(item, "text", None) is not None
    )


def structured(result: Any) -> dict[str, Any]:
    value = result.structured_content
    assert isinstance(value, dict)
    return value


def test_readiness_marker_fails_soft_when_probe_raises(capsys: Any) -> None:
    class FailingSession:
        async def probe_health(self) -> Any:
            raise RuntimeError("probe exploded")

    composition = SimpleNamespace(
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        session_manager=FailingSession(),
    )

    _emit_readiness_marker(composition)

    marker = capsys.readouterr().err
    assert "SUPERVISOR_READY status=UNAVAILABLE" in marker


def test_readiness_marker_reports_ready(capsys: Any) -> None:
    class ReadySession:
        async def probe_health(self) -> BackendHealth:
            return BackendHealth(
                capability="local_codex_bridge",
                status=BackendHealthStatus.READY,
                user_message="Codex is ready.",
                repairable=False,
            )

    composition = SimpleNamespace(
        profile="lightweight",
        workspace_backend="devspace",
        agent_backend="local_codex_bridge",
        session_manager=ReadySession(),
    )

    _emit_readiness_marker(composition)

    marker = capsys.readouterr().err
    assert "SUPERVISOR_READY status=READY" in marker


def test_create_read_and_resume_task_through_mcp(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    service = MemoryService(database)
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "GAME-301",
                    "title": "Outer systems",
                    "repository": "cuteyuchen/game",
                    "goal": "Implement peripheral game systems.",
                    "hard_constraints": [
                        "Core gameplay must not depend on ads before ad capability is available."
                    ],
                },
            )
            assert created.is_error is False
            created_data = structured(created)
            assert created_data["task"]["task_id"] == "GAME-301"
            assert created_data["task"]["revision"] == 1

            read = await client.call_tool("get_supervised_task", {"task_id": "GAME-301"})
            assert read.is_error is False
            assert structured(read)["task"]["revision"] == 1

            resumed = await client.call_tool(
                "resume_supervised_task",
                {"task_id": "GAME-301", "mode": "resume"},
            )
            assert resumed.is_error is False
            resumed_data = structured(resumed)
            assert resumed_data["task"]["revision"] == 1
            assert "Implement peripheral game systems." in resumed_data["content"]
            assert "Core gameplay must not depend on ads" in resumed_data["content"]

            # Resume is read-only and must not invalidate the revision it reports.
            after_resume = await client.call_tool(
                "get_supervised_task",
                {"task_id": "GAME-301"},
            )
            assert structured(after_resume)["task"]["revision"] == 1

    try:
        run(scenario())
    finally:
        service.close()


def test_mutation_returns_latest_revision_and_stale_call_is_model_readable_error() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {"task_id": "TASK-REV", "title": "Revision test"},
            )
            revision = structured(created)["task"]["revision"]
            assert revision == 0

            override = await client.call_tool(
                "record_user_override",
                {
                    "task_id": "TASK-REV",
                    "expected_revision": revision,
                    "instruction": "Reuse the current panel.",
                },
            )
            assert override.is_error is False
            assert structured(override)["task"]["revision"] == 1

            stale = await client.call_tool(
                "update_task_intent",
                {
                    "task_id": "TASK-REV",
                    "expected_revision": 0,
                    "goal": "This stale mutation must not apply.",
                },
            )
            assert stale.is_error is True
            assert "STALE_CONTEXT" in text_content(stale)

            current = await client.call_tool(
                "get_supervised_task",
                {"task_id": "TASK-REV"},
            )
            current_data = structured(current)["task"]
            assert current_data["revision"] == 1
            assert current_data["current_goal"] is None

    try:
        run(scenario())
    finally:
        service.close()


def test_superseded_decision_is_searchable_but_not_current_context() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {"task_id": "TASK-DEC", "title": "Decision lifecycle"},
            )
            revision = structured(created)["task"]["revision"]

            old = await client.call_tool(
                "add_task_decision",
                {
                    "task_id": "TASK-DEC",
                    "expected_revision": revision,
                    "title": "Old storage model",
                    "content": "Use three save slots.",
                },
            )
            old_data = structured(old)
            old_id = old_data["decision"]["decision_id"]
            revision = old_data["task"]["revision"]

            new = await client.call_tool(
                "add_task_decision",
                {
                    "task_id": "TASK-DEC",
                    "expected_revision": revision,
                    "title": "Current storage model",
                    "content": "Use one save only.",
                },
            )
            new_data = structured(new)
            new_id = new_data["decision"]["decision_id"]
            revision = new_data["task"]["revision"]

            superseded = await client.call_tool(
                "supersede_task_decision",
                {
                    "task_id": "TASK-DEC",
                    "expected_revision": revision,
                    "decision_id": old_id,
                    "superseded_by": new_id,
                },
            )
            assert structured(superseded)["decision"]["status"] == "SUPERSEDED"

            context = await client.call_tool(
                "get_context_pack",
                {"task_id": "TASK-DEC"},
            )
            context_text = structured(context)["content"]
            assert "Use one save only." in context_text
            assert "Use three save slots." not in context_text

            search = await client.call_tool(
                "search_task_memory",
                {"task_id": "TASK-DEC", "query": "three save slots"},
            )
            hits = structured(search)["hits"]
            old_hits = [item for item in hits if item["source_id"] == old_id]
            assert old_hits
            assert old_hits[0]["status"] == "SUPERSEDED"

    try:
        run(scenario())
    finally:
        service.close()


def test_plan_gate_through_mcp() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            created = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "TASK-PLAN",
                    "title": "Plan gate",
                    "goal": "Implement feature X.",
                },
            )
            revision = structured(created)["task"]["revision"]

            plan_result = await client.call_tool(
                "create_task_plan",
                {
                    "task_id": "TASK-PLAN",
                    "expected_revision": revision,
                    "content": "1. Inspect\n2. Test\n3. Implement",
                },
            )
            plan_data = structured(plan_result)
            assert plan_data["plan"]["status"] == "DRAFT"
            assert plan_data["task"]["phase"] == "plan_review"

            approved = await client.call_tool(
                "approve_task_plan",
                {
                    "task_id": "TASK-PLAN",
                    "expected_revision": plan_data["task"]["revision"],
                    "plan_id": plan_data["plan"]["plan_id"],
                },
            )
            approved_data = structured(approved)
            assert approved_data["plan"]["status"] == "APPROVED"
            assert approved_data["task"]["phase"] == "implementing"

            context = await client.call_tool(
                "get_context_pack",
                {"task_id": "TASK-PLAN", "mode": "resume"},
            )
            assert "Plan V1 / APPROVED" in structured(context)["content"]

    try:
        run(scenario())
    finally:
        service.close()


def test_unknown_task_is_tool_error_not_false_success() -> None:
    service = MemoryService()
    server = create_mcp_server(service)

    async def scenario() -> None:
        async with Client(server) as client:
            result = await client.call_tool(
                "get_supervised_task",
                {"task_id": "DOES-NOT-EXIST"},
            )
            assert result.is_error is True
            assert "Unknown supervised task" in text_content(result)
            assert result.structured_content is None

    try:
        run(scenario())
    finally:
        service.close()


def test_new_server_process_model_can_resume_same_database(tmp_path: Path) -> None:
    database = tmp_path / "resume-mcp.db"

    first_service = MemoryService(database)
    first_server = create_mcp_server(first_service)

    async def create() -> None:
        async with Client(first_server) as client:
            result = await client.call_tool(
                "create_supervised_task",
                {
                    "task_id": "TASK-RESUME",
                    "title": "Cross conversation resume",
                    "goal": "Recover without old ChatGPT history.",
                    "hard_constraints": ["Latest user override always wins."],
                },
            )
            assert result.is_error is False

    run(create())
    first_service.close()

    second_service = MemoryService(database)
    second_server = create_mcp_server(second_service)

    async def resume() -> None:
        async with Client(second_server) as client:
            result = await client.call_tool(
                "resume_supervised_task",
                {"task_id": "TASK-RESUME"},
            )
            assert result.is_error is False
            data = structured(result)
            assert "Recover without old ChatGPT history." in data["content"]
            assert "Latest user override always wins." in data["content"]

    try:
        run(resume())
    finally:
        second_service.close()
