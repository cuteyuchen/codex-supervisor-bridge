# P6 — Human Override and Hard Replan

## Goal

P6 makes a major user direction change safe while development is in progress.

A hard override must not be implemented as an unstructured prompt like "stop and do something else". The system first records the new user intent, captures the old implementation state, confirms the active Codex write turn has stopped, classifies existing work, invalidates the old plan, and only then creates a new read-only plan.

## Required lifecycle

```text
IMPLEMENTING
  -> HARD OVERRIDE
  -> capture Work Snapshot
  -> Intent Version + 1
  -> supersede active plan
  -> interrupt active Codex turn
  -> SNAPSHOT_READY
  -> classify KEEP / MODIFY / DROP
  -> READY_TO_PLAN
  -> new read-only Plan workflow
  -> PLAN_REVIEW
  -> explicit plan approval
  -> implementation may resume
```

If the active turn cannot be confirmed interrupted, the task fails closed in `BLOCKED` and must be reconciled before a new writer is allowed.

## Durable records

### Work Snapshot

A snapshot anchors the state that existed immediately before the new intent took control. It records, where available:

- task revision;
- intent version;
- plan version and previous approved/draft plan;
- prior goal and phase;
- Kandev task identity;
- Git branch / HEAD;
- latest checkpoint;
- Codex workflow / operation / thread / turn identity;
- remote runtime status;
- changed files;
- validation summary;
- evidence references;
- KEEP / MODIFY / DROP classification and notes.

The snapshot is audit evidence. It is not rewritten into the new task state.

### Hard Replan

The replan record links:

- the old intent version;
- the new target intent version;
- the Work Snapshot;
- the superseded plan;
- the new user goal and reason;
- interrupt status/error;
- the new read-only planning workflow;
- the eventual replacement plan.

## Atomic intent switch

`begin_hard_replan(...)` is revision-protected.

Within one write transaction it:

1. rejects stale `expected_revision`;
2. supersedes any previous active hard-replan flow for the task;
3. captures the pre-override Work Snapshot;
4. supersedes current DRAFT/APPROVED plans and updates their search status;
5. changes the current user goal;
6. increments Intent Version;
7. sets the task to `PAUSED`;
8. marks Codex runtime state `interrupt_pending` when runtime identity exists;
9. appends `USER_OVERRIDE`, `INTENT_UPDATED`, and plan-supersession audit events.

This prevents the old plan from remaining current after the new user intent becomes authoritative.

## Interrupt confirmation

The external Codex interrupt is deliberately not hidden inside the database transaction.

The Supervisor flow is:

1. persist the hard override and Work Snapshot;
2. request interrupt through the Agent/Codex integration;
3. finalize the interrupt result with a fresh revision check.

Success:

- replan -> `SNAPSHOT_READY`;
- task remains `PAUSED`;
- next action is Work Snapshot classification.

Failure / uncertain unreconciled state:

- replan -> `INTERRUPT_FAILED`;
- task -> `BLOCKED`;
- runtime next action -> reconcile/confirm runtime state;
- no new implementation may start.

## KEEP / MODIFY / DROP classification

Before a new plan is created, the Supervisor classifies partial work into three disjoint groups:

- **KEEP** — remains valid under the new intent;
- **MODIFY** — useful but must be adjusted;
- **DROP** — conflicts with the new intent or should not be carried forward.

The groups must be disjoint. Classification is revision-protected and moves the task into `REPLANNING` / `READY_TO_PLAN`.

This mechanism avoids both dangerous extremes:

- blindly resetting all partial work;
- blindly carrying all partial work into the new architecture.

## New-plan gate

After classification, a new Codex Plan Mode workflow may start. It must remain read-only.

The new plan must be tied to the current Intent Version and may not silently revive a superseded plan. Normal P4 Plan Gate rules continue to apply: local Supervisor approval is required before implementation can resume.

## MCP surface

P6 exposes semantic hard-replan operations instead of direct database mutation. The surface is expected to cover:

- begin hard replan;
- retry/reconcile interrupt when required;
- inspect current hard-replan state;
- inspect Work Snapshot;
- classify KEEP/MODIFY/DROP;
- start the replacement plan;
- continue through normal plan review/approval.

All mutating operations use `expected_revision`.

## Interaction with revised P6.5 execution modes

P6 was first implemented while Codex was the only writer. P6.5 generalizes it without discarding the P6 model.

When `active_writer=CODEX`, a hard replan requires native turn interruption before classification.

When `active_writer=CHATGPT`, the same high-level lifecycle applies but the interrupt step becomes a direct-writer quiesce/release operation:

```text
stop direct mutations
  -> capture diff / validation / command state
  -> release CHATGPT write lease
  -> classify snapshot
  -> replan
```

Therefore Work Snapshot, Intent Version, old-plan supersession, KEEP/MODIFY/DROP, and stale-revision protection remain backend-neutral Supervisor Core behavior.

## Tests / acceptance

P6 should not merge until CI covers at least:

- schema migration/reopen for Work Snapshot + Hard Replan records;
- hard override increments Intent Version and task revision;
- old plan becomes superseded immediately;
- Work Snapshot contains pre-override state;
- stale revision is rejected;
- successful interrupt opens classification;
- failed interrupt blocks planning/implementation;
- KEEP/MODIFY/DROP overlap is rejected;
- classified snapshot survives store reopen;
- a new read-only planning workflow can be attached only at the correct lifecycle point;
- MCP surface cannot skip required gates.
