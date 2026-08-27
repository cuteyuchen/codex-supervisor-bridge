from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field

from codex_supervisor_bridge.memory.service import MemoryService

from .kandev_client import KandevAdapter, extract_kandev_task_id
from .kandev_models import (
    KandevCapabilities,
    KandevCreateTaskRequest,
    KandevTaskBinding,
)


class KandevProvisionOptions(BaseModel):
    parent_id: str | None = None
    workspace_id: str | None = None
    workflow_id: str | None = None
    workflow_step_id: str | None = None
    workspace_mode: str | None = None
    agent_profile_id: str | None = None
    executor_profile_id: str | None = None
    repository_id: str | None = None
    local_path: str | None = None
    repository_url: str | None = None
    base_branch: str | None = None
    blocked_by: list[str] = Field(default_factory=list)
    start_when_unblocked: bool | None = None


AdapterFactory = Callable[[], KandevAdapter]


class KandevCoordinator:
    """Maps canonical supervisor tasks onto Kandev without giving Kandev ownership of intent."""

    def __init__(self, memory: MemoryService, adapter_factory: AdapterFactory) -> None:
        self.memory = memory
        self.adapter_factory = adapter_factory

    async def capabilities(self) -> KandevCapabilities:
        async with self.adapter_factory() as adapter:
            return await adapter.capabilities()

    @staticmethod
    def external_id_for(supervisor_task_id: str) -> str:
        return f"codex-supervisor-bridge:{supervisor_task_id}"

    async def provision_task(
        self,
        task_id: str,
        expected_revision: int,
        *,
        options: KandevProvisionOptions | None = None,
    ) -> KandevTaskBinding:
        task = self.memory.assert_revision(task_id, expected_revision)
        external_id = self.external_id_for(task_id)

        if task.external_kandev_task_id:
            return KandevTaskBinding(
                supervisor_task_id=task_id,
                kandev_task_id=task.external_kandev_task_id,
                external_id=external_id,
                created_or_reused=True,
                remote_payload={},
            )

        options = options or KandevProvisionOptions()
        request = KandevCreateTaskRequest(
            title=task.title[:60],
            prompt=task.current_goal or task.current_state or task.title,
            parent_id=options.parent_id,
            workspace_id=options.workspace_id,
            workflow_id=options.workflow_id,
            workflow_step_id=options.workflow_step_id,
            workspace_mode=options.workspace_mode,
            autopilot=False,
            agent_profile_id=options.agent_profile_id,
            executor_profile_id=options.executor_profile_id,
            # P3 intentionally prepares Kandev but does not start an agent turn.
            # P4 owns supervised Codex startup and live steering.
            start_agent=False,
            repository_id=options.repository_id,
            local_path=options.local_path,
            repository_url=options.repository_url,
            base_branch=options.base_branch,
            external_id=external_id,
            blocked_by=options.blocked_by,
            start_when_unblocked=options.start_when_unblocked,
        )

        async with self.adapter_factory() as adapter:
            remote = await adapter.create_task(request)
        kandev_task_id = extract_kandev_task_id(remote)

        # This may raise STALE_CONTEXT if the user changed the task while the
        # remote call was in flight. That is intentional. Replaying this method
        # with the new revision reuses the same Kandev task through external_id.
        self.memory.bind_kandev_task(
            task_id,
            expected_revision,
            kandev_task_id,
            external_id=external_id,
        )
        return KandevTaskBinding(
            supervisor_task_id=task_id,
            kandev_task_id=kandev_task_id,
            external_id=external_id,
            created_or_reused=True,
            remote_payload=remote,
        )

    async def list_linked_sessions(self, task_id: str) -> dict[str, Any]:
        task = self.memory.get_task(task_id)
        if not task.external_kandev_task_id:
            return {"task_id": task_id, "linked": False, "sessions": []}
        async with self.adapter_factory() as adapter:
            payload = await adapter.list_task_sessions(task.external_kandev_task_id)
        return {
            "task_id": task_id,
            "kandev_task_id": task.external_kandev_task_id,
            "linked": True,
            "remote": payload,
        }

    async def get_linked_conversation(
        self,
        task_id: str,
        *,
        session_id: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        task = self.memory.get_task(task_id)
        if not task.external_kandev_task_id:
            return {"task_id": task_id, "linked": False, "conversation": []}
        async with self.adapter_factory() as adapter:
            payload = await adapter.get_task_conversation(
                task.external_kandev_task_id,
                session_id=session_id,
                limit=limit,
            )
        return {
            "task_id": task_id,
            "kandev_task_id": task.external_kandev_task_id,
            "linked": True,
            "remote": payload,
        }
