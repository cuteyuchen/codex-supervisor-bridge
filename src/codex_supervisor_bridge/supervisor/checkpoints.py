from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from codex_supervisor_bridge.backends.agent import AgentBackend
from codex_supervisor_bridge.backends.models import AgentSnapshot, PlanHandle
from codex_supervisor_bridge.integrations.codex_coordinator import CodexCoordinator
from codex_supervisor_bridge.memory.checkpoint_models import (
    CheckpointCreateResult,
    CheckpointType,
    CodexCheckpoint,
)
from codex_supervisor_bridge.memory.checkpoint_store import create_checkpoint, latest_checkpoint
from codex_supervisor_bridge.memory.codex_runtime import get_codex_runtime
from codex_supervisor_bridge.memory.service import MemoryService

_GATE_STATUS = {
    "blocked",
    "failed",
    "error",
    "aborted",
    "cancelled",
    "canceled",
    "waiting_for_approval",
    "waiting_for_user_input",
}
_TERMINAL_SUCCESS = {"completed", "complete", "succeeded", "success", "done"}
_GATE_WORDS = re.compile(
    r"\b(blocked|blocker|failed|failure|error|approval|required input|permission|"
    r"schema|migration|public api|contract|architecture|security|auth|dependency|"
    r"scope change|deviation|dangerous)\b",
    re.IGNORECASE,
)
_PROGRESS_METHOD_WORDS = (
    "item/completed",
    "item/failed",
    "turn/diff/updated",
    "turn/completed",
    "turn/failed",
    "item/status/updated",
)
_TEST_FAILED = re.compile(r"\b(test|tests|pytest|lint|build|check)[^\n]{0,80}\b(fail|failed|error)", re.I)
_TEST_PASSED = re.compile(r"\b(test|tests|pytest|lint|build|check)[^\n]{0,80}\b(pass|passed|green|success)", re.I)


@dataclass(frozen=True)
class NormalizedProgress:
    checkpoint_type: CheckpointType
    trigger_reason: str
    source_fingerprint: str
    remote_status: str | None
    next_action: str | None
    completed: list[str]
    in_progress: list[str]
    files_changed: list[str]
    validation: dict[str, Any]
    assumptions: list[str]
    deviations: list[str]
    blockers: list[str]
    risks: list[str]
    next_steps: list[str]
    raw_event_count: int


def _value(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _string(payload: dict[str, Any], *keys: str) -> str | None:
    value = _value(payload, *keys)
    return value if isinstance(value, str) and value.strip() else None


def _list(payload: dict[str, Any], *keys: str) -> list[Any]:
    value = _value(payload, *keys)
    return value if isinstance(value, list) else []


def _clip(text: str, limit: int = 280) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _event_method(event: dict[str, Any]) -> str:
    return str(_value(event, "method", "eventType", "event_type", "type", "kind") or "")


def _event_text(event: dict[str, Any]) -> str:
    for key in ("summary", "message", "text", "description", "delta"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return _clip(value)
    params = event.get("params")
    if isinstance(params, dict):
        for key in ("summary", "message", "text", "description", "delta"):
            value = params.get(key)
            if isinstance(value, str) and value.strip():
                return _clip(value)
    method = _event_method(event)
    return method or "Codex progress event"


def _paths(value: Any, *, output: list[str], depth: int = 0) -> None:
    if depth > 4 or len(output) >= 20:
        return
    if isinstance(value, dict):
        for key, item in value.items():
            lower = str(key).lower()
            if lower in {"path", "file", "filepath", "file_path", "filename"}:
                if isinstance(item, str) and item.strip():
                    output.append(_clip(item, 180))
            elif lower in {"files", "changedfiles", "changed_files", "paths"}:
                _paths(item, output=output, depth=depth + 1)
            elif isinstance(item, (dict, list)):
                _paths(item, output=output, depth=depth + 1)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                output.append(_clip(item, 180))
            else:
                _paths(item, output=output, depth=depth + 1)


def _unique(values: list[str], limit: int = 12) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _high_signal_events(operation: dict[str, Any]) -> list[dict[str, Any]]:
    events = _list(operation, "progressEvents", "progress_events")
    result: list[dict[str, Any]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        method = _event_method(raw).lower()
        text = _event_text(raw)
        if any(marker in method for marker in _PROGRESS_METHOD_WORDS) or _GATE_WORDS.search(text):
            result.append(raw)
    return result[-20:]


def normalize_progress_snapshot(
    operation: dict[str, Any],
    workflow: dict[str, Any] | None,
    pending: dict[str, Any] | None,
    *,
    previous: CodexCheckpoint | None,
    force_heartbeat: bool,
    now: datetime | None = None,
) -> NormalizedProgress:
    workflow = workflow or {}
    pending = pending or {}
    status = _string(operation, "status") or _string(workflow, "status", "phase")
    next_action = _string(operation, "nextRecommendedAction", "next_action") or _string(
        workflow, "nextRecommendedAction", "next_action"
    )

    last_error = _string(operation, "lastError", "last_error") or _string(
        workflow, "lastError", "last_error"
    )
    interactions = _list(pending, "interactions", "pendingInteractions", "pending_interactions")
    events = _high_signal_events(operation)

    completed: list[str] = []
    in_progress: list[str] = []
    blockers: list[str] = []
    assumptions: list[str] = []
    deviations: list[str] = []
    risks: list[str] = []
    validation_signals: list[str] = []
    files: list[str] = []
    gate_reasons: list[str] = []

    for event in events:
        method = _event_method(event).lower()
        text = _event_text(event)
        _paths(event, output=files)
        if "completed" in method:
            completed.append(text)
        elif "failed" in method:
            blockers.append(text)
            gate_reasons.append("Codex reported a failed item/turn")
        elif "diff" in method:
            in_progress.append(text)
        elif "status" in method:
            in_progress.append(text)
        if _TEST_FAILED.search(text):
            blockers.append(text)
            validation_signals.append(text)
            gate_reasons.append("validation failure detected")
        elif _TEST_PASSED.search(text):
            validation_signals.append(text)
        if _GATE_WORDS.search(text):
            gate_reasons.append("high-risk progress signal detected")
        lower = text.lower()
        if "assum" in lower:
            assumptions.append(text)
        if "deviat" in lower or "scope change" in lower:
            deviations.append(text)
        if "risk" in lower or "warning" in lower:
            risks.append(text)

    if last_error:
        blockers.append(_clip(last_error))
        gate_reasons.append("Codex runtime exposes lastError")
    if interactions:
        kinds = [
            str(item.get("kind") or item.get("type") or "interaction")
            for item in interactions
            if isinstance(item, dict)
        ]
        blockers.append("Pending Codex interaction: " + ", ".join(_unique(kinds, 5)))
        gate_reasons.append("Codex requires approval or user input")
    if (status or "").lower() in _GATE_STATUS:
        gate_reasons.append(f"runtime status is {status}")
    if (status or "").lower() in _TERMINAL_SUCCESS:
        completed.append(f"Codex turn reached terminal status: {status}")

    validation: dict[str, Any] = {}
    if validation_signals:
        validation["signals"] = _unique(validation_signals, 6)
        validation["status"] = "failed" if any(_TEST_FAILED.search(x) for x in validation_signals) else "passed"

    previous_status = previous.remote_status if previous else None
    previous_action = previous.next_action if previous else None
    status_changed = previous is not None and bool(status and status != previous_status)
    action_changed = previous is not None and bool(next_action and next_action != previous_action)
    material_progress = bool(
        completed
        or in_progress
        or files
        or validation
        or status_changed
        or action_changed
    )
    if gate_reasons:
        checkpoint_type = CheckpointType.GATE
        trigger = "; ".join(_unique(gate_reasons, 4))
    elif material_progress:
        checkpoint_type = CheckpointType.PROGRESS
        trigger = "meaningful Codex progress/state change"
    else:
        checkpoint_type = CheckpointType.HEARTBEAT
        trigger = "forced heartbeat" if force_heartbeat else "no high-signal progress change"

    next_steps = [next_action] if next_action else []
    normalized = {
        "type": checkpoint_type.value,
        "status": status,
        "next_action": next_action,
        "completed": _unique(completed),
        "in_progress": _unique(in_progress),
        "files": _unique(files),
        "validation": validation,
        "assumptions": _unique(assumptions),
        "deviations": _unique(deviations),
        "blockers": _unique(blockers),
        "risks": _unique(risks),
        "next_steps": next_steps,
        "interaction_count": len(interactions),
    }
    if checkpoint_type == CheckpointType.HEARTBEAT:
        instant = (now or datetime.now(timezone.utc)).timestamp()
        normalized["heartbeat_bucket"] = int(instant // 180)
    fingerprint = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return NormalizedProgress(
        checkpoint_type=checkpoint_type,
        trigger_reason=trigger,
        source_fingerprint=fingerprint,
        remote_status=status,
        next_action=next_action,
        completed=normalized["completed"],
        in_progress=normalized["in_progress"],
        files_changed=normalized["files"],
        validation=validation,
        assumptions=normalized["assumptions"],
        deviations=normalized["deviations"],
        blockers=normalized["blockers"],
        risks=normalized["risks"],
        next_steps=next_steps,
        raw_event_count=len(_list(operation, "progressEvents", "progress_events")),
    )


def normalize_agent_snapshot(
    snapshot: AgentSnapshot,
    *,
    previous: CodexCheckpoint | None,
    force_heartbeat: bool,
    now: datetime | None = None,
) -> NormalizedProgress:
    """Classify an already provider-neutral AgentSnapshot for checkpoint storage."""

    status = snapshot.status or None
    gate_reasons: list[str] = []
    if snapshot.pending_interactions:
        gate_reasons.append("Codex requires approval or user input")
    if snapshot.blockers:
        gate_reasons.append("Agent snapshot contains blockers")
    if (status or "").lower() in _GATE_STATUS or (status or "").upper() == "UNKNOWN":
        gate_reasons.append(f"runtime status is {status}")
    validation_status = str(snapshot.validation.get("status") or "").lower()
    if validation_status in {"failed", "failure", "error"}:
        gate_reasons.append("validation failure detected")

    previous_status = previous.remote_status if previous else None
    material_progress = bool(
        snapshot.completed
        or snapshot.in_progress
        or snapshot.files_changed
        or snapshot.validation
        or snapshot.assumptions
        or snapshot.deviations
        or snapshot.risks
        or snapshot.next_steps
        or (previous is not None and status and status != previous_status)
    )
    if gate_reasons:
        checkpoint_type = CheckpointType.GATE
        trigger = "; ".join(_unique(gate_reasons, 4))
    elif material_progress:
        checkpoint_type = CheckpointType.PROGRESS
        trigger = "meaningful agent progress/state change"
    else:
        checkpoint_type = CheckpointType.HEARTBEAT
        trigger = "forced heartbeat" if force_heartbeat else "no high-signal progress change"

    normalized = {
        "type": checkpoint_type.value,
        "status": status,
        "completed": snapshot.completed,
        "in_progress": snapshot.in_progress,
        "files": snapshot.files_changed,
        "validation": snapshot.validation,
        "assumptions": snapshot.assumptions,
        "deviations": snapshot.deviations,
        "blockers": snapshot.blockers,
        "risks": snapshot.risks,
        "next_steps": snapshot.next_steps,
        "interaction_count": len(snapshot.pending_interactions),
    }
    if checkpoint_type == CheckpointType.HEARTBEAT:
        instant = (now or datetime.now(timezone.utc)).timestamp()
        normalized["heartbeat_bucket"] = int(instant // 180)
    fingerprint = hashlib.sha256(
        json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return NormalizedProgress(
        checkpoint_type=checkpoint_type,
        trigger_reason=trigger,
        source_fingerprint=fingerprint,
        remote_status=status,
        next_action=snapshot.next_steps[0] if snapshot.next_steps else None,
        completed=_unique(snapshot.completed),
        in_progress=_unique(snapshot.in_progress),
        files_changed=_unique(snapshot.files_changed),
        validation=snapshot.validation,
        assumptions=_unique(snapshot.assumptions),
        deviations=_unique(snapshot.deviations),
        blockers=_unique(snapshot.blockers),
        risks=_unique(snapshot.risks),
        next_steps=_unique(snapshot.next_steps),
        raw_event_count=snapshot.raw_event_count,
    )


class CheckpointService:
    def __init__(
        self,
        memory: MemoryService,
        codex: CodexCoordinator | None = None,
        *,
        agent_backend: AgentBackend | None = None,
    ) -> None:
        if codex is None and agent_backend is None:
            raise ValueError("codex or agent_backend is required")
        self.memory = memory
        self.codex = codex
        self.agent_backend = agent_backend

    async def collect(
        self,
        task_id: str,
        *,
        force_heartbeat: bool = False,
    ) -> CheckpointCreateResult:
        task = self.memory.get_task(task_id)
        runtime = get_codex_runtime(self.memory.store, task_id)
        operation: dict[str, Any] = {}
        workflow: dict[str, Any] | None = None
        pending: dict[str, Any] | None = None
        snapshot: AgentSnapshot | None = None
        if runtime is not None and self.agent_backend is not None:
            snapshot = await self.agent_backend.observe(
                PlanHandle(
                    operation_id=runtime.operation_id,
                    workflow_id=runtime.workflow_id,
                    thread_id=runtime.thread_id,
                    turn_id=runtime.turn_id,
                    status=runtime.remote_status or "unknown",
                )
            )
        elif runtime is not None:
            assert self.codex is not None
            async with self.codex.adapter_factory() as adapter:
                if runtime.operation_id:
                    operation = await adapter.get_operation_status(runtime.operation_id)
                if runtime.workflow_id:
                    workflow = await adapter.get_workflow_status(runtime.workflow_id)
                pending = await adapter.list_pending_interactions(
                    workflow_id=runtime.workflow_id,
                    operation_id=runtime.operation_id,
                    thread_id=runtime.thread_id,
                    turn_id=runtime.turn_id,
                )
        previous = latest_checkpoint(self.memory.store, task_id)
        if snapshot is not None:
            normalized = normalize_agent_snapshot(
                snapshot,
                previous=previous,
                force_heartbeat=force_heartbeat,
            )
        else:
            normalized = normalize_progress_snapshot(
                operation,
                workflow,
                pending,
                previous=previous,
                force_heartbeat=force_heartbeat,
            )
        runtime_values = {
            "workflow_id": snapshot.workflow_id if snapshot else runtime.workflow_id if runtime else None,
            "operation_id": snapshot.operation_id if snapshot else runtime.operation_id if runtime else None,
            "thread_id": snapshot.thread_id if snapshot else runtime.thread_id if runtime else None,
            "turn_id": snapshot.turn_id if snapshot else runtime.turn_id if runtime else None,
            "remote_status": normalized.remote_status,
            "next_action": normalized.next_action,
        }
        evidence_refs = list(snapshot.evidence_refs) if snapshot else []
        if runtime and runtime.operation_id:
            evidence_refs.append(f"codex-operation:{runtime.operation_id}")
        if runtime and runtime.workflow_id:
            evidence_refs.append(f"codex-workflow:{runtime.workflow_id}")
        return create_checkpoint(
            self.memory.store,
            task_id,
            task.revision,
            checkpoint_type=normalized.checkpoint_type,
            source_fingerprint=normalized.source_fingerprint,
            trigger_reason=normalized.trigger_reason,
            runtime=runtime_values,
            completed=normalized.completed,
            in_progress=normalized.in_progress,
            files_changed=normalized.files_changed,
            validation=normalized.validation,
            assumptions=normalized.assumptions,
            deviations=normalized.deviations,
            blockers=normalized.blockers,
            risks=normalized.risks,
            next_steps=normalized.next_steps,
            evidence_refs=evidence_refs,
            raw_event_count=normalized.raw_event_count,
        )
