from __future__ import annotations

from pathlib import Path

import pytest

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.memory.context_pack import ContextPackBuilder
from codex_supervisor_bridge.memory.errors import StaleRevisionError
from codex_supervisor_bridge.memory.models import (
    ConstraintSeverity,
    ContextPackMode,
    DecisionStatus,
    EvidenceType,
    PlanStatus,
    SummaryType,
)
from codex_supervisor_bridge.memory.service import MemoryService
from codex_supervisor_bridge.memory.store import MemoryStore


def test_task_persists_across_reopen(tmp_path: Path) -> None:
    database = tmp_path / "supervisor.db"
    with MemoryStore(database) as store:
        created = store.create_task(
            "GAME-213",
            "Single save system",
            repository="cuteyuchen/game",
            goal="Implement a single-save architecture.",
        )
        assert created.revision == 0
        store.add_constraint(
            created.task_id,
            created.revision,
            "Reuse the existing StorageManager.",
            severity=ConstraintSeverity.HARD,
        )
        after_constraint = store.get_task(created.task_id)
        assert after_constraint.revision == 1

    with MemoryStore(database) as reopened:
        restored = reopened.get_task("GAME-213")
        assert restored.repository == "cuteyuchen/game"
        assert restored.current_goal == "Implement a single-save architecture."
        assert restored.revision == 1
        constraints = reopened.active_constraints(restored.task_id)
        assert [item.content for item in constraints] == ["Reuse the existing StorageManager."]
        assert current_schema_version(reopened._conn) == 1


def test_stale_revision_is_rejected_without_partial_write() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-1", "Revision lock")
        store.add_decision(task.task_id, task.revision, "Architecture", "Use SQLite.")
        current = store.get_task(task.task_id)
        assert current.revision == 1

        with pytest.raises(StaleRevisionError) as error:
            store.add_constraint(
                task.task_id,
                0,
                "This mutation is stale.",
            )

        assert error.value.expected_revision == 0
        assert error.value.current_revision == 1
        assert store.active_constraints(task.task_id) == []
        assert store.get_task(task.task_id).revision == 1


def test_superseded_decision_is_excluded_from_context_pack() -> None:
    with MemoryStore() as store:
        task = store.create_task(
            "TASK-2",
            "Context decisions",
            goal="Build the save feature.",
        )
        old = store.add_decision(
            task.task_id,
            task.revision,
            "Save slots",
            "Support three save slots.",
        )
        task = store.get_task(task.task_id)
        new = store.add_decision(
            task.task_id,
            task.revision,
            "Save model",
            "Support one save only.",
        )
        task = store.get_task(task.task_id)
        store.supersede_decision(
            task.task_id,
            task.revision,
            old.decision_id,
            superseded_by=new.decision_id,
        )
        task = store.get_task(task.task_id)
        store.add_constraint(
            task.task_id,
            task.revision,
            "Do not replace StorageManager.",
            severity=ConstraintSeverity.HARD,
        )

        pack = ContextPackBuilder(store).build(task.task_id)

        assert "Support one save only." in pack.content
        assert "Support three save slots." not in pack.content
        assert "Do not replace StorageManager." in pack.content
        assert store.active_decisions(task.task_id)[0].status == DecisionStatus.ACTIVE


def test_plan_lifecycle_and_context_mode() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-3", "Plan gate", goal="Implement feature X.")
        plan = store.create_plan(task.task_id, task.revision, "1. Inspect\n2. Test\n3. Implement")
        task = store.get_task(task.task_id)
        assert task.plan_version == 1
        assert plan.status == PlanStatus.DRAFT

        review_pack = ContextPackBuilder(store).build(
            task.task_id,
            mode=ContextPackMode.PLAN_REVIEW,
        )
        assert "Plan V1 / DRAFT" in review_pack.content

        approved = store.approve_plan(task.task_id, task.revision, plan.plan_id)
        task = store.get_task(task.task_id)
        assert approved.status == PlanStatus.APPROVED
        assert store.approved_plan(task.task_id) is not None
        assert task.phase.value == "implementing"

        resume_pack = ContextPackBuilder(store).build(task.task_id)
        assert "Plan V1 / APPROVED" in resume_pack.content


def test_summary_evidence_and_context_snapshot_do_not_advance_revision() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-4", "Derived memory")
        baseline_revision = task.revision

        store.add_summary(
            task.task_id,
            SummaryType.CURRENT_STATE,
            0,
            0,
            "Nothing implemented yet.",
        )
        store.add_evidence(
            task.task_id,
            EvidenceType.TEST_LOG,
            "pytest",
            "42 tests passed.",
        )
        ContextPackBuilder(store).build(task.task_id, persist_snapshot=True)

        assert store.get_task(task.task_id).revision == baseline_revision
        events = store.list_events(task.task_id)
        assert len(events) >= 4


def test_context_budget_never_drops_mandatory_hard_constraint() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-5", "Budget", goal="Keep mandatory state.")
        hard_text = "HARD-NEVER-DROP " + ("x" * 5000)
        store.add_constraint(
            task.task_id,
            task.revision,
            hard_text,
            severity=ConstraintSeverity.HARD,
        )
        task = store.get_task(task.task_id)
        for index in range(8):
            store.add_evidence(
                task.task_id,
                EvidenceType.CODEX_PROGRESS,
                "codex",
                f"optional-{index} " + ("y" * 2000),
            )

        pack = ContextPackBuilder(
            store,
            target_chars=3000,
            hard_max_chars=7000,
        ).build(task.task_id)

        assert "HARD-NEVER-DROP" in pack.content
        assert pack.token_estimate > 0
        assert pack.truncated_sections


def test_memory_search_returns_active_and_historical_records() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-6", "Search")
        decision = store.add_decision(
            task.task_id,
            task.revision,
            "Storage architecture",
            "Reuse StorageManager and avoid a second persistence abstraction.",
        )
        task = store.get_task(task.task_id)
        store.supersede_decision(task.task_id, task.revision, decision.decision_id)

        hits = store.search(task.task_id, "StorageManager")

        assert hits
        assert hits[0].kind in {"decision", "task"}
        decision_hits = [hit for hit in hits if hit.source_id == decision.decision_id]
        assert decision_hits
        assert decision_hits[0].status == "SUPERSEDED"


def test_memory_service_resume_works_without_previous_chat(tmp_path: Path) -> None:
    database = tmp_path / "resume.db"
    with MemoryService(database) as service:
        task = service.create_task(
            "TASK-7",
            "Resume",
            goal="Resume from durable state.",
            hard_constraints=["Latest user override has priority."],
        )
        revision = task.revision

    with MemoryService(database) as new_process:
        pack = new_process.resume_task("TASK-7")
        assert pack.task.revision == revision
        assert "Resume from durable state." in pack.content
        assert "Latest user override has priority." in pack.content
