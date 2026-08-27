# P5 — Checkpoints and Supervisor Review

## Goal

P5 turns high-frequency Codex runtime activity into low-frequency, bounded, durable decision points for ChatGPT Supervisor.

The browser conversation must not ingest full Codex transcripts or event streams just to answer "is the work still on track?". Instead the Bridge produces a structured checkpoint containing only the facts needed for supervision, while retaining pointers to the upstream workflow/operation as evidence references.

## Flow

```text
Codex Control Plane
  operation/workflow/pending interactions
           |
           v
Checkpoint Aggregator
  filter low-value deltas
  normalize high-signal events
  classify + fingerprint
           |
           v
HEARTBEAT / PROGRESS / GATE
           |
           v
Persistent Checkpoint Store
           |
           v
Context Pack
           |
           v
ChatGPT Supervisor Review
  CONTINUE / STEER / INTERRUPT / REPLAN / ACCEPT
```

## Checkpoint classes

### HEARTBEAT

An observation that says the runtime is still present but no new high-signal progress requires a decision.

- does not advance task revision;
- does not move the task into Supervisor review;
- is bucketed/deduplicated to avoid heartbeat spam;
- may be forced by a future periodic worker.

### PROGRESS

A meaningful implementation change such as:

- an item/turn completed;
- a meaningful status transition;
- a diff/file change signal;
- validation result;
- a changed recommended next action.

PROGRESS advances task revision and requires Supervisor review.

### GATE

A decision-sensitive event such as:

- failure/blocker/error;
- pending Codex approval or user input;
- validation failure;
- architecture/schema/public API/security/dependency/scope-deviation signal;
- other high-risk progress event.

GATE advances task revision and requires immediate Supervisor review.

The first classifier is deterministic and conservative. It deliberately does not invoke another LLM inside the Bridge.

## Noise filtering

The aggregator does not fingerprint ordinary token/reasoning/text deltas. High-signal Codex progress methods currently include completion/failure, diff updates, turn completion/failure, and meaningful status updates.

Only bounded summaries are persisted into checkpoint fields. A raw reasoning delta must never appear in the default Context Pack merely because it appeared in upstream progress events.

## Durable schema

Schema v3 adds:

- `codex_checkpoints`
- `checkpoint_reviews`

A checkpoint stores:

- sequence and type;
- task / intent / plan versions;
- workflow / operation / thread / turn identifiers;
- remote status and next action;
- trigger reason;
- completed / in-progress items;
- changed files;
- validation summary;
- assumptions / deviations / blockers / risks;
- next steps;
- evidence references;
- raw event count only, not the raw event stream;
- source fingerprint;
- whether review is required.

Reviews are stored separately so the historical Codex observation and the Supervisor decision remain independently auditable.

## Revision semantics

- read-only Codex status polling: no revision change;
- HEARTBEAT creation: no revision change;
- PROGRESS/GATE creation: task revision +1 and phase becomes `supervisor_review`;
- checkpoint review: task revision +1;
- only the latest unreviewed decision checkpoint can be reviewed.

This prevents an old GPT review from overwriting a newer checkpoint while avoiding revision churn from simple status refreshes.

## Review decisions

`review_codex_checkpoint` records exactly one of:

- `CONTINUE`
- `STEER`
- `INTERRUPT`
- `REPLAN`
- `ACCEPT`

The review records policy but does not silently execute a remote control action.

Recommended explicit follow-up:

| Decision | Next action |
| --- | --- |
| CONTINUE | none; Codex keeps running |
| STEER | `soft_steer_codex` with the recorded instruction |
| INTERRUPT | `interrupt_codex` |
| REPLAN | `interrupt_codex` now; P6 will provide atomic hard-replan |
| ACCEPT | continue to final-review policy |

A STEER review requires a concrete instruction.

## Context Pack integration

`LATEST CODEX CHECKPOINT` now renders the structured checkpoint instead of replaying recent raw `CODEX_PROGRESS` event JSON.

The section includes only bounded supervision facts and review status. Evidence references point back to the Codex operation/workflow when deeper inspection is necessary.

## ChatGPT-facing tools

- `collect_codex_checkpoint`
- `get_latest_codex_checkpoint`
- `list_codex_checkpoints`
- `review_codex_checkpoint`

Raw upstream progress/status tools remain behind the Bridge.

## Acceptance criteria

P5 must prove:

- schema v2 migrates to v3 and checkpoints survive reopen;
- heartbeat does not advance revision;
- identical heartbeat/current snapshot is deduplicated;
- progress advances revision and requires review;
- pending interaction or validation failure becomes GATE;
- raw reasoning/text deltas do not leak into Context Pack;
- only the newest unreviewed checkpoint may be reviewed;
- stale review is rejected by revision lock;
- STEER requires an instruction and recommends `soft_steer_codex`;
- checkpoint tools have correct read/write MCP annotations;
- raw upstream status/progress tools are not exposed to ChatGPT;
- Python 3.12 and 3.13 lint/tests are green.

## Deferred

P5 does not yet provide an autonomous polling loop. P8 will schedule repeated collection/review. P6 adds atomic hard replan, work snapshots, and KEEP/MODIFY/DROP classification after an interrupt.
