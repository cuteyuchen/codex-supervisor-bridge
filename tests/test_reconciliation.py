from __future__ import annotations

import base64
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from codex_supervisor_bridge.bootstrap import (
    AppDataPaths,
    ReconciliationService,
)
from codex_supervisor_bridge.mcp.server import build_parser


def _paths(canonical: Path, legacy: Path) -> AppDataPaths:
    return AppDataPaths(
        root=canonical,
        data=canonical / "data",
        logs=canonical / "logs",
        runtime=canonical / "runtime",
        config=canonical / "config",
        cache=canonical / "cache",
        components=canonical / "components",
        legacy_roots=(legacy,),
        resolution_source="test",
    )


def _settings(path: Path, *, project: str = "C:/canonical") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "config_version": 1,
                "basic": {"project_directory": project},
            }
        ),
        encoding="utf-8",
    )


def _database(path: Path, *, active_writer: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', '8')"
        )
        connection.execute(
            "CREATE TABLE supervised_tasks (task_id TEXT PRIMARY KEY, status TEXT)"
        )
        connection.execute(
            "CREATE TABLE task_execution_state (task_id TEXT, active_writer TEXT)"
        )
        connection.execute(
            "CREATE TABLE task_backend_binding (task_id TEXT PRIMARY KEY)"
        )
        if active_writer is not None:
            connection.execute(
                "INSERT INTO task_execution_state(task_id, active_writer) VALUES('task', ?)",
                (active_writer,),
            )
        connection.commit()


def _service(paths: AppDataPaths, *, pid_exists=None) -> ReconciliationService:
    return ReconciliationService(
        paths=paths,
        clock=lambda: datetime(2026, 8, 29, 9, 30, tzinfo=timezone.utc),
        nonce_factory=lambda: "test-nonce",
        pid_exists=pid_exists or (lambda pid: False),
    )


def test_split_brain_dry_run_contains_relations_and_safe_plan(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json", project="C:/legacy")
    _database(canonical / "data" / "supervisor.db")
    (legacy / "config" / "secrets").mkdir(parents=True)
    (legacy / "config" / "secrets" / "old-token.dpapi").write_bytes(b"opaque-value")

    plan = _service(_paths(canonical, legacy)).plan(legacy_root=legacy)

    assert plan.safe_to_apply is True
    assert plan.selected_authority == "canonical"
    assert plan.settings_relation == "DIFFERENT"
    assert plan.secret_name_relation == "LEGACY_ONLY"
    assert plan.database_relation == "CANONICAL_ONLY"
    assert plan.other_persistent_categories == ["settings", "secrets"]
    assert plan.plan_id.startswith("rpl1_")
    assert plan.secret_name_comparison["same_names"] == []
    assert plan.secret_name_comparison["legacy_only_names"] == ["old-token"]
    assert plan.secret_values_compared is False
    assert "opaque-value" not in json.dumps(plan.model_dump(mode="json"))


def test_plan_id_rejects_changed_root_before_apply(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    service = _service(_paths(canonical, legacy))
    plan = service.plan(legacy_root=legacy)
    (legacy / "config" / "settings.json").write_text(
        json.dumps({"config_version": 1, "basic": {"codex_enabled": False}}),
        encoding="utf-8",
    )

    result = service.apply(
        plan_id=plan.plan_id,
        selected_authority="canonical",
        legacy_root=legacy,
    )

    assert result.status == "STALE_RECONCILIATION_PLAN"
    assert not (legacy / ".codex-supervisor-legacy-inactive.json").exists()


def test_same_name_secrets_compare_names_only_not_ciphertext(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    for root, value in ((canonical, b"canonical-ciphertext"), (legacy, b"legacy-ciphertext")):
        directory = root / "config" / "secrets"
        directory.mkdir(parents=True)
        (directory / "shared.dpapi").write_bytes(value)

    plan = _service(_paths(canonical, legacy)).plan(legacy_root=legacy)
    rendered = json.dumps(plan.model_dump(mode="json"), ensure_ascii=True)

    assert plan.secret_name_relation == "IDENTICAL"
    assert plan.secret_name_comparison["same_names"] == ["shared"]
    assert plan.secret_values_compared is False
    assert "canonical-ciphertext" not in rendered
    assert "legacy-ciphertext" not in rendered


def test_legacy_database_active_runtime_blocks_apply(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    _database(legacy / "data" / "supervisor.db")
    with sqlite3.connect(legacy / "data" / "supervisor.db") as connection:
        connection.execute("CREATE TABLE codex_runtime_state (remote_status TEXT)")
        connection.execute("INSERT INTO codex_runtime_state(remote_status) VALUES('running')")
        connection.commit()

    plan = _service(_paths(canonical, legacy)).plan(legacy_root=legacy)

    assert plan.safe_to_apply is False
    assert "RECONCILIATION_BLOCKED_ACTIVE_STATE" in plan.blocking_reasons


def test_invalid_plan_timestamp_fails_closed(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    service = _service(_paths(canonical, legacy))
    plan = service.plan(legacy_root=legacy)
    encoded = plan.plan_id.removeprefix("rpl1_")
    payload = json.loads(
        base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
    )
    payload["created_at"] = "not-a-timestamp"
    malformed = "rpl1_" + base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")

    result = service.apply(
        plan_id=malformed,
        selected_authority="canonical",
        legacy_root=legacy,
    )

    assert result.status == "INVALID_RECONCILIATION_PLAN"
    assert result.blocking_reasons == ["INVALID_RECONCILIATION_PLAN"]


def test_explicit_canonical_apply_backups_then_marks_inactive(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    canonical_settings = (canonical / "config" / "settings.json").read_bytes()
    _settings(legacy / "config" / "settings.json", project="C:/legacy")
    _database(canonical / "data" / "supervisor.db")
    (legacy / "runtime").mkdir(parents=True)
    (legacy / "runtime" / "processes.json").write_text(
        json.dumps({"bridge": {"status": "STOPPED", "pid": None}}),
        encoding="utf-8",
    )
    (legacy / "config" / "secrets").mkdir(parents=True)
    (legacy / "config" / "secrets" / "old-token.dpapi").write_bytes(b"opaque-value")

    paths = _paths(canonical, legacy)
    service = _service(paths)
    plan = service.plan(legacy_root=legacy)
    result = service.apply(
        plan_id=plan.plan_id,
        selected_authority="canonical",
        legacy_root=legacy,
    )

    assert result.status == "RECONCILED"
    assert result.applied is True
    assert result.backup_location is not None
    backup = Path(result.backup_location)
    assert (backup / "config" / "settings.json").is_file()
    assert (backup / "config" / "secrets" / "old-token.dpapi").is_file()
    assert (legacy / ".codex-supervisor-legacy-inactive.json").is_file()
    marker = json.loads((legacy / ".codex-supervisor-legacy-inactive.json").read_text())
    assert marker["authority"] == "canonical"
    assert marker["reconciliation_plan_id"] == plan.plan_id
    assert "opaque-value" not in json.dumps(marker)
    assert (canonical / "config" / "settings.json").read_bytes() == canonical_settings
    assert paths.root_report.status == "CLEAN"


def test_apply_failure_keeps_split_brain_active_after_backup(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    paths = _paths(canonical, legacy)
    service = _service(paths)
    plan = service.plan(legacy_root=legacy)

    def fail_marker(**kwargs):
        assert Path(kwargs["backup"]).is_dir()
        raise OSError("marker write test failure")

    service._write_inactive_marker = fail_marker  # type: ignore[method-assign]
    result = service.apply(
        plan_id=plan.plan_id,
        selected_authority="canonical",
        legacy_root=legacy,
    )

    assert result.status == "RECONCILIATION_FAILED"
    assert not (legacy / ".codex-supervisor-legacy-inactive.json").exists()
    assert paths.root_report.status == "SPLIT_BRAIN_DETECTED"
    assert list((canonical / ".reconciliation-backups").iterdir())


def test_legacy_database_active_writer_blocks_apply(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    _database(legacy / "data" / "supervisor.db", active_writer="CODEX")

    plan = _service(_paths(canonical, legacy)).plan(legacy_root=legacy)

    assert plan.safe_to_apply is False
    assert "RECONCILIATION_BLOCKED_ACTIVE_STATE" in plan.blocking_reasons


def test_live_legacy_runtime_blocks_apply(tmp_path: Path) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    (legacy / "runtime").mkdir(parents=True)
    (legacy / "runtime" / "processes.json").write_text(
        json.dumps({"bridge": {"status": "RUNNING", "pid": 4321}}),
        encoding="utf-8",
    )

    plan = _service(_paths(canonical, legacy), pid_exists=lambda pid: pid == 4321).plan(
        legacy_root=legacy
    )

    assert plan.safe_to_apply is False
    assert "BLOCKED_LIVE_LEGACY_RUNTIME" in plan.blocking_reasons


def test_second_reconciliation_is_idempotent_and_root_remains_discoverable(
    tmp_path: Path,
) -> None:
    canonical = tmp_path / "canonical"
    legacy = tmp_path / "legacy"
    _settings(canonical / "config" / "settings.json")
    _settings(legacy / "config" / "settings.json")
    _database(canonical / "data" / "supervisor.db")
    paths = _paths(canonical, legacy)
    service = _service(paths)
    first = service.plan(legacy_root=legacy)
    service.apply(plan_id=first.plan_id, selected_authority="canonical", legacy_root=legacy)
    second = service.plan(legacy_root=legacy)
    result = service.apply(
        plan_id=second.plan_id,
        selected_authority="canonical",
        legacy_root=legacy,
    )

    assert second.already_inactive is True
    assert result.status == "ALREADY_RECONCILED"
    assert result.idempotent is True
    assert result.root_report is not None
    assert result.root_report["legacy_roots"][0]["inactive"] is True


def test_reconcile_cli_parser_accepts_two_phase_flags() -> None:
    args = build_parser().parse_args(
        [
            "reconcile-app-data",
            "--dry-run",
            "--keep",
            "canonical",
            "--legacy-root",
            "C:/legacy",
            "--advanced",
            "--json",
        ]
    )

    assert args.command == "reconcile-app-data"
    assert args.dry_run is True
    assert args.keep == "canonical"
    assert args.legacy_root == Path("C:/legacy")
