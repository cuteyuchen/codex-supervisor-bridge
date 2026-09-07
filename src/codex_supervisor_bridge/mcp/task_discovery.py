from __future__ import annotations

from codex_supervisor_bridge.memory.service import MemoryService

from .models import TaskDiscoverySummary


def list_task_summaries(
    service: MemoryService,
    *,
    active_only: bool = True,
    repository: str | None = None,
    limit: int = 20,
) -> list[TaskDiscoverySummary]:
    """Read recent canonical Supervisor tasks without enumerating agent-native threads.

    Task discovery intentionally stays at the Supervisor memory boundary.  The
    result omits native Codex thread/turn ids, Git hashes, backend ids, and other
    provider-specific details; callers can request a task-scoped Context Pack
    after selecting a task id.
    """

    clauses: list[str] = []
    params: list[object] = []
    if active_only:
        clauses.append("status = ?")
        params.append("active")
    if repository is not None:
        clauses.append("repository = ?")
        params.append(repository)

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(limit)
    query = (
        "SELECT * FROM supervised_tasks"
        f"{where} ORDER BY updated_at DESC, task_id ASC LIMIT ?"
    )

    store = service.store
    # Discovery is a read-only projection over the same canonical SQLite store.
    # Keep the query under MemoryStore's lock so it has the same thread-safety
    # guarantees as other memory reads.  No task revision or event is changed.
    with store._lock:  # noqa: SLF001 - same-package read-only projection
        rows = store._conn.execute(query, params).fetchall()  # noqa: SLF001
        tasks = [store._task_from_row(row) for row in rows]  # noqa: SLF001

    return [TaskDiscoverySummary.from_task(task) for task in tasks]
