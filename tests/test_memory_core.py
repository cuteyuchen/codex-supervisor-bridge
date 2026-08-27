from __future__ import annotations

from pathlib import Path

import pytest

from codex_supervisor_bridge.db import current_schema_version
from codex_supervisor_bridge.db.schema import SCHEMA_VERSION
from codex_supervisor_bridge.memory.context_pack import ContextPackBuilder
from codex_supervisor_bridge.memory.errors import StaleRevisionError
from codex_supervisor_bridge.memory.models import (
    Actor,
    ConstraintSeverity,
    ContextPackMode,
    DecisionStatus,
    EventType,
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
        assert current_schema_version(reopened._conn) == SCHEMA_VERSION


def test_stale_revision_is_rejected_without_partial_write() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-1", "Revision lock")
        store.add_decision(task.task_id, task.revision, "Architecture", "Use SQLite.")
        current = store.get_task(task.task_id)
        assert current.revision == 1

        with pytest.raises(StaleRevisionError) as error:
            store.add_constraint(task.task_id, 0, "This mutation is stale.")
        assert error.value.expected_revision == 0
        assert error.value.current_revision == 1
        assert store.active_constraints(task.task_id) == []
        assert store.get_task(task.task_id).revision == 1


def test_public_event_append_advances_revision_and_is_auditable() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-EVENT", "Event API")
        event = store.record_event(
            task.task_id,
            task.revision,
            Actor.USER,
            EventType.USER_OVERRIDE,
            {"instruction": "Reuse the existing panel."},
        )
        assert event.revision == 1
        assert event.event_type == EventType.USER_OVERRIDE
        assert store.get_task(task.task_id).revision == 1


def test_superseded_decision_is_not_rendered_as_active_context() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-2", "Decision lifecycle")
        old = store.add_decision(
            task.task_id,
            task.revision,
            "Storage architecture",
            "Use an obsolete multi-slot design.",
        )
        current = store.get_task(task.task_id)
        new = store.add_decision(
            task.task_id,
            current.revision,
            "Storage architecture v2",
            "Use the existing single-slot StorageManager.",
        )
        current = store.get_task(task.task_id)
        store.supersede_decision(
            task.task_id,
            current.revision,
            old.decision_id,
            superseded_by=new.decision_id,
        )

        active = store.active_decisions(task.task_id)
        assert [item.decision_id for item in active] == [new.decision_id]
        assert old.content not in ContextPackBuilder(store).build(task.task_id).content
        hits = store.search(task.task_id, "obsolete multi-slot")
        assert hits
        assert hits[0].status == DecisionStatus.SUPERSEDED.value


def test_hard_constraint_survives_tiny_context_budget() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-3", "Context budget")
        hard_text = "Never rewrite StorageManager. " + ("critical " * 200)
        store.add_constraint(
            task.task_id,
            task.revision,
            hard_text,
            severity=ConstraintSeverity.HARD,
        )
        builder = ContextPackBuilder(store, target_chars=400, hard_max_chars=600)
        pack = builder.build(task.task_id)
        assert hard_text in pack.content
        assert pack.over_budget_due_to_mandatory is True


def test_plan_lifecycle_keeps_only_current_approved_plan() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-4", "Plan lifecycle")
        plan1 = store.create_plan(task.task_id, task.revision, "Plan alpha")
        current = store.get_task(task.task_id)
        store.approve_plan(task.task_id, current.revision, plan1.plan_id)

        current = store.get_task(task.task_id)
        plan2 = store.create_plan(task.task_id, current.revision, "Plan beta")
        current = store.get_task(task.task_id)
        store.approve_plan(task.task_id, current.revision, plan2.plan_id)

        assert store.approved_plan(task.task_id).plan_id == plan2.plan_id
        hits = store.search(task.task_id, "Plan alpha")
        assert hits and hits[0].status == PlanStatus.SUPERSEDED.value
        pack = ContextPackBuilder(store).build(task.task_id)
        assert "Plan beta" in pack.content
        assert "Plan alpha" not in pack.content


def test_derived_data_does_not_churn_revision() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-5", "Derived memory")
        store.add_summary(
            task.task_id,
            SummaryType.CURRENT_STATE,
            0,
            0,
            "Implementation has not started.",
        )
        store.add_evidence(
            task.task_id,
            EvidenceType.GIT_DIFF,
            "git",
            "No changed files.",
        )
        store.save_context_snapshot(
            task.task_id,
            ContextPackMode.RESUME,
            "snapshot",
            1,
        )
        assert store.get_task(task.task_id).revision == 0


def test_search_finds_historical_evidence() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-6", "Search")
        store.add_evidence(
            task.task_id,
            EvidenceType.TEST_LOG,
            "pytest",
            "Migration test failed on legacy save version 2.",
        )
        hits = store.search(task.task_id, "legacy save")
        assert hits
        assert hits[0].kind == "evidence"


def test_1000_events_remain_auditable_and_context_is_bounded() -> None:
    with MemoryStore() as store:
        task = store.create_task("TASK-7", "Event volume", goal="Ship the feature.")
        for index in range(1000):
            event = store.record_event(
                task.task_id,
                store.get_task(task.task_id).revision,
                Actor.SYSTEM,
                EventType.STATE_RECONCILED,
                {"index": index, "detail": "runtime fact"},
            )
            assert event.revision == index + 1
        events = store.list_events(task.task_id, limit=1100)
        assert len(events) == 1002  # TASK_CREATED + INTENT_CREATED + 1000 state events
        assert events[-1].payload["index"] == 999
        pack = ContextPackBuilder(store).build(task.task_id)
        assert len(pack.content) <= 64_000
