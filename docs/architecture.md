# Architecture

## Purpose

Codex Supervisor Bridge lets ChatGPT act as a persistent development supervisor while Codex performs implementation work, Kandev manages development workflow/worktrees, and GitHub remains the source of code/PR/CI facts.

The bridge exists because neither a browser chat nor a long-lived coding-agent thread is a reliable system of record for multi-day development.

## Target topology

```text
User
  |
  v
ChatGPT Web Supervisor
  |
  | Remote MCP
  v
Codex Supervisor Bridge
  |-- Persistent Memory / Context Packs
  |-- Revision Guard
  |-- Checkpoint Aggregation
  |-- User Override / Drift Policy
  |
  +-------------------+
  |                   |
  v                   v
Kandev             Codex Control Plane
  |                   |
  |                   +-- codex app-server
  |                   +-- thread/turn
  |                   +-- turn/steer
  |                   +-- turn/interrupt
  |                   +-- progress/approval/recovery
  |
  +-- task/workflow
  +-- plan/worktree
  +-- review/QA
  +-- PR/CI workflow
  |
  +----------+--------+
             |
             v
            Git
             |
             v
           GitHub
```

## Source-of-truth ownership

A single fact should have one authoritative owner.

### Supervisor Bridge owns

- current user intent and `intent_version`;
- active user constraints;
- supervisor decisions;
- supervisor task `revision`;
- context snapshots;
- checkpoint/review decisions;
- references to external evidence;
- cross-system reconciliation state.

### Kandev owns

- development task/workflow structure;
- workflow step/stage;
- worktree lifecycle;
- development plan document UI/workflow integration;
- review/QA/PR/CI workflow orchestration.

The bridge may cache identifiers and compact summaries but must not invent independent worktree truth.

### Codex Control Plane owns

- Codex app-server process state;
- Codex thread ID;
- active turn ID and turn state;
- runtime progress events;
- pending Codex approvals/questions;
- native Codex interruption/steering/recovery facts.

### GitHub owns

- branch/commit history visible on the remote;
- pull requests;
- CI/check-run facts;
- review facts created in GitHub.

## Instruction priority

The supervisory policy is ordered:

1. Safety policy
2. Latest user override
3. Active HARD constraints
4. Current user intent
5. Current approved plan
6. Supervisor checkpoint instruction
7. Codex local implementation plan
8. Codex autonomous implementation choice

Lower layers may not silently override higher layers.

## Optimistic revision control

Every future mutating ChatGPT-facing MCP tool will accept `expected_revision`.

```text
ChatGPT reads Rev 42
       |
       | user issues a newer override
       v
Task becomes Rev 43
       |
old ChatGPT response sends CONTINUE(expected_revision=42)
       |
       v
STALE_CONTEXT — mutation rejected
```

This protects the newest user instruction from delayed model/tool calls.

## Task lifecycle

Initial target state machine:

```text
CREATED
  -> CONTEXT_READY
  -> PLANNING
  -> PLAN_REVIEW
       -> PLANNING          (rejected)
       -> IMPLEMENTING      (approved)
  -> CHECKPOINT_PENDING
  -> SUPERVISOR_REVIEW
       -> IMPLEMENTING      (continue/steer)
       -> PAUSED            (interrupt)
       -> REPLANNING        (hard replan)
       -> CODE_REVIEW       (implementation complete)
  -> QA
  -> PR
  -> CI
  -> FINAL_REVIEW
       -> IMPLEMENTING      (changes required)
       -> ACCEPTED
```

A user override can arrive in any non-terminal state.

## Soft steer vs hard replan

### Soft steer

Use when intent and approved architecture remain valid.

Example: reuse the existing settings panel instead of creating a new dialog.

Future execution:

```text
USER_OVERRIDE event
-> revision increment
-> constraint/decision update as appropriate
-> Codex turn/steer
-> continue current turn
```

### Hard replan

Use when goal, architecture, acceptance criteria, or core behavior materially changes.

Future execution:

```text
USER_OVERRIDE
-> intent_version + 1
-> current plan superseded
-> turn/interrupt
-> work snapshot
-> REPLANNING
-> Codex Plan Mode
-> ChatGPT plan review
-> new plan approved
-> implementation resumes
```

## Context strategy

ChatGPT chat history is never assumed to be durable project memory.

A new conversation resumes with a generated Context Pack containing current truth and compact evidence pointers. Raw historical events stay outside the default context and are retrieved progressively when needed.

This prevents both context-window exhaustion and recursive-summary drift.

## MCP security boundary

ChatGPT will connect only to Supervisor Bridge.

The public supervisor MCP must expose semantic operations such as:

- create/resume/read task;
- read/search memory;
- approve/reject plan;
- review checkpoint;
- continue/steer/interrupt/replan;
- retrieve evidence.

It must **not** expose an arbitrary shell, unrestricted filesystem, or raw process execution API to ChatGPT.

Kandev, Codex Control Plane, and GitHub integrations remain behind the bridge.

## Delivery phases

- **P1** Persistent Memory + Context Pack
- **P2** ChatGPT Remote MCP surface
- **P3** Kandev adapter
- **P4** Codex live control / native turn steering
- **P5** checkpoint aggregation and supervisor review
- **P6** user override / hard replan / snapshot workflow
- **P7** review, QA, PR, and CI integration
- **P8** automated supervised development loop

The system should remain useful at each phase; later phases extend supervision without replacing the P1 durable-memory foundation.
