# P4 — Codex Live Control

## Goal

P4 connects Supervisor Bridge to `codex-control-plane-mcp` without exposing the upstream control plane directly to ChatGPT. The Bridge remains the authority for user intent, revision locks and plan approval; the upstream control plane remains the authority for Codex app-server runtime, thread/turn identity, durable operations and pending interactions.

The milestone is successful when a supervised task can move through:

```text
Supervisor Context
  -> Codex read-only Plan Mode
  -> poll plan workflow
  -> import latest valid plan as local DRAFT
  -> explicit local approval
  -> remote-plan equality recheck
  -> workspace-write execution
  -> live status / approvals
  -> mid-turn steer OR interrupt
```

without giving the ChatGPT-facing MCP caller arbitrary Codex operations, shell access or sandbox escalation.

## Ownership boundaries

| State | Owner |
| --- | --- |
| latest user intent / constraints | Supervisor Bridge |
| task / intent / plan revision | Supervisor Bridge |
| local plan approval | Supervisor Bridge |
| workflow/worktree | Kandev |
| Codex operation/workflow/thread/turn | Codex Control Plane |
| Codex approvals/questions | Codex Control Plane |
| code / commit / PR / CI facts | GitHub / Kandev |

Supervisor Bridge stores only the minimum Codex identity required to recover control after ChatGPT or the Bridge process restarts.

## Runtime topology

P4 is designed for the upstream control plane's central-worker mode:

```text
ChatGPT Web
   |
   v
Supervisor Bridge MCP
   |
   | short-lived client-mode MCP gateway
   v
codex-control-plane-mcp
   |
   | shared state DB / CODEX_HOME
   v
central worker
   |
   v
codex app-server
```

`Supervisor Bridge` starts the upstream MCP gateway with:

```text
CODEX_MCP_EXECUTION_MODE=client
```

The central worker is a separate long-running process. Therefore restarting the Bridge or changing ChatGPT conversations must not imply killing the active Codex turn.

## Contract gate

Before any upstream write/control action, `CodexControlAdapter` verifies `codex_health_summary`:

- `ok == true`
- `version.serverName == codex-control-plane-mcp`
- `version.contractVersion == 1`
- non-empty `toolSurfaceHash`
- non-empty `guideHash`
- all required stable tools are advertised

A failed contract check blocks Plan, execution, steer and interrupt. This is intentional: silently guessing an incompatible upstream tool contract is more dangerous than refusing to run.

## Durable runtime identity

Database schema v2 adds `codex_runtime_state`:

- `task_id`
- `workflow_id`
- `operation_id`
- `thread_id`
- `turn_id`
- `remote_status`
- `next_action`
- `last_client_request_id`
- `updated_at`

Control transitions are persisted under the same optimistic task-revision transaction that records the audit event. Read-only status polling does **not** increase task revision.

This avoids a common failure mode where merely refreshing status makes the supervising ChatGPT context stale.

## Plan gate

### Start

`start_codex_plan` always starts Plan Mode with:

- `sandbox=read-only`
- `approval_policy=on-request`
- current Supervisor Context Pack embedded in the planning instruction
- deterministic `client_request_id`

It never starts implementation.

### Import

`import_codex_plan` polls `codex_get_workflow_status` and accepts only `latestPlan` with:

```text
planQuality = valid_plan
```

The plan is stored as a local Supervisor `DRAFT` authored by Codex.

### Approval

Existing Supervisor tool `approve_task_plan` is the explicit approval gate.

### Execute

`execute_codex_approved_plan`:

1. requires a local `APPROVED` plan;
2. reads the upstream current `latestPlan` again;
3. compares normalized remote plan content to the local approved plan;
4. refuses execution with `CODEX_PLAN_GATE` if they differ;
5. only then calls upstream `codex_approve_plan`;
6. forces `sandbox=workspace-write`.

ChatGPT cannot pass a sandbox argument to this tool. `danger-full-access` is not part of the exposed Supervisor surface.

## Soft steering

`soft_steer_codex` is for a local correction while the approved plan remains valid.

It requires the persisted current `thread_id` and `turn_id` and calls upstream:

```text
codex_submit_task
operation_type=steer_turn
thread_id=<current thread>
expected_turn_id=<current turn>
message=<supervisor correction>
client_request_id=<stable id>
```

This adds context to the current turn rather than creating a second turn.

A material architecture/scope change is not a soft steer. It should be handled by interrupt + hard replan in P6.

## Interrupt

`interrupt_codex` targets the persisted workflow/operation/thread/turn identity. On successful upstream interruption the local task moves to `PAUSED` and an immutable `CODEX_INTERRUPTED` event is appended.

## Revision-race compensation

A remote Codex write and a user override can cross in flight:

```text
Supervisor checks revision 41
  -> remote Plan / Execute / Steer starts
  -> user override advances local task to revision 42
  -> old remote response returns
```

The Bridge must not persist the old remote action as if revision 41 were still current, and it must not leave that remote action running silently.

P4 therefore treats remote-write binding as fail-closed:

1. every remote write is followed by the normal optimistic local revision check;
2. if the bind reports `STALE_CONTEXT`, the Bridge immediately issues a compensating `codex_interrupt_turn` using the remote workflow/operation/thread/turn identifiers it just received;
3. if compensation succeeds, the original `STALE_CONTEXT` is re-raised so the Supervisor must reload the newest user state;
4. if compensation itself fails, the Bridge raises `CODEX_COMPENSATION_REQUIRED` rather than claiming the remote action was safely stopped.

This compensation applies to Plan start, approved-plan execution, soft steer and interaction answers. A direct interrupt does not need a second compensation interrupt: if its local bind races, the remote side is already stopped and the caller simply receives stale-context recovery semantics.

## Pending interactions

P4 exposes bounded tools to:

- list pending Codex approvals/questions for the supervised runtime;
- answer exactly one interaction;
- record the answer as a revision-protected control event.

Raw approval internals are not re-exported.

## ChatGPT-facing P4 tools

Read-only:

- `get_codex_control_health`
- `get_codex_runtime_capabilities`
- `preflight_codex_project`
- `get_codex_status`
- `list_codex_pending_interactions`

Mutating:

- `start_codex_plan`
- `import_codex_plan`
- `execute_codex_approved_plan`
- `soft_steer_codex`
- `interrupt_codex`
- `answer_codex_pending_interaction`

Not exposed:

- raw `codex_submit_task`
- raw `codex_approve_plan`
- raw `codex_interrupt_turn`
- arbitrary command/process execution
- arbitrary sandbox selection
- direct Codex SQLite/transcript writes

## Error model

Expected cross-system failures use bounded `CodexControlError` subclasses and become model-readable MCP `ToolError` messages.

Unexpected internal exceptions remain redacted by the MCP SDK.

Important expected errors include:

- `CODEX_CONTRACT_ERROR`
- `CODEX_TOOL_ERROR`
- `CODEX_PLAN_GATE`
- `CODEX_COMPENSATION_REQUIRED`
- existing `STALE_CONTEXT`

## Idempotency

Long-running/durable upstream writes use deterministic `client_request_id` values, including Plan, execution and steer requests. A retry caused by transport loss should refer to the same logical operation instead of launching duplicate work.

## Recovery

After Bridge restart:

1. reopen the Supervisor SQLite database;
2. recover `codex_runtime_state`;
3. reconnect through client-mode control-plane MCP;
4. query the saved workflow/operation;
5. continue status/review/steer/interrupt from the latest remote truth.

No ChatGPT conversation history is required for this recovery path.

## P4 acceptance tests

Automated tests must prove:

- wrong control-plane server/contract is rejected;
- schema v1 migrates forward to schema v2;
- Codex runtime identity survives database reopen;
- Plan Mode is read-only;
- status reads do not bump Supervisor revision;
- only `valid_plan` can be imported;
- local approval is mandatory before execution;
- remote plan drift blocks execution and performs no write call;
- execution is fixed to `workspace-write`;
- soft steer targets the current thread and expected active turn;
- pending interaction listing is read-only;
- interaction answers are revision protected;
- interrupt moves the task to `PAUSED`;
- a user revision race after a remote write triggers compensation interrupt;
- failed compensation is escalated and never reported as a safe stop;
- raw upstream write tools and sandbox selection are absent from the ChatGPT-facing MCP surface;
- Python 3.12 and 3.13 lint/tests are green.

## Deferred to P5/P6

P4 deliberately does not yet implement:

- automatic progress-event aggregation into Supervisor checkpoints;
- GPT checkpoint policy (`CONTINUE / STEER / INTERRUPT / REPLAN`);
- hard-replan work snapshots and KEEP/MODIFY/DROP classification;
- unattended supervision loops;
- Kandev Review / QA / PR / CI fixup orchestration.

Those layers build on P4's durable runtime and safe control primitives.
