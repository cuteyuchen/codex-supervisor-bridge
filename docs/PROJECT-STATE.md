# Project State and Current Architecture Baseline

> **Read this first when resuming the project in a new ChatGPT conversation.**
>
> This document records the current product intent and the architecture decisions that supersede earlier fixed-backend assumptions. It is deliberately more durable than a browser conversation.

Last updated: 2026-08-27

## Product goal

Codex Supervisor Bridge lets a user manage local software development from ChatGPT Web without making the browser conversation the source of truth.

The system must support all of these working styles:

1. **ChatGPT Web develops directly on the local computer.**
2. **ChatGPT Web develops first and delegates selected work to Codex when useful.**
3. **ChatGPT Web supervises Codex for the whole implementation.**

The user may change style during an active task. A switch of executor must not lose task memory, approved decisions, constraints, Git/worktree continuity, validation state, or audit history.

## Non-negotiable product constraint: low learning cost

The implementation may use multiple local components, but that complexity must not become user-facing configuration complexity.

The normal target experience is:

```text
install
  -> select project
  -> connect ChatGPT
  -> describe the task
```

The user should **not** need to learn or manually manage concepts such as:

- MCP transport details;
- service ports;
- SQLite paths;
- worker processes;
- DevSpace state directories;
- Kandev endpoints;
- Codex thread / turn / workflow IDs;
- backend selection;
- lease TTLs or checkpoint intervals.

Default configuration should be automatic. Advanced values exist for diagnostics and exceptional cases only.

### User-facing settings budget

The first useful version should expose only settings that materially affect user intent, for example:

- project directory;
- default development style: Automatic / Web-first / Codex-first;
- whether ChatGPT may delegate to Codex automatically;
- whether local commands may run automatically or require approval;
- whether Git commits may be created automatically;
- whether draft PRs may be created automatically;
- GitHub connection status;
- Codex availability/status.

Backend names should normally be hidden. The user should see capability health such as `Local workspace: Ready` or `Codex: Ready`, not `Kandev MCP URL` or `Control Plane worker`.

Errors should prefer a user-facing diagnosis and recovery action, with technical detail expandable on demand.

## Supervisor Core is the product-specific source of truth

The following capabilities belong to Codex Supervisor Bridge and must not be replaced by DevSpace, Local-Codex-Bridge, Kandev, or a Codex-native thread:

- supervised task identity;
- current user goal / intent;
- Intent Version;
- Plan Version;
- monotonic Task Revision and `expected_revision` protection;
- active HARD constraints;
- decision registry;
- append-only task event timeline;
- Context Pack generation;
- structured supervisor checkpoints;
- user override history;
- hard replan lifecycle;
- Work Snapshot and KEEP / MODIFY / DROP classification;
- cross-ChatGPT-conversation resume.

A native Codex `thread_id` is runtime identity, not permanent task identity.

## Execution modes

Detailed rules live in `docs/P6.5-execution-modes.md`.

### DIRECT

ChatGPT Web is the active developer and may use a constrained local workspace surface to read, edit, run commands, validate, and inspect changes.

Codex is not started automatically.

### HYBRID

ChatGPT Web may develop directly, then hand off bounded work to Codex, supervise it, and take the task back afterwards.

This is expected to be the default **Automatic / Web-first when appropriate** experience for a single-user installation.

### CODEX_SUPERVISED

ChatGPT Web acts as product owner, architect, supervisor, and reviewer; mutating implementation work is performed by Codex.

## Single-writer invariant

Direct ChatGPT editing and Codex must not mutate the same worktree concurrently by default.

Every supervised task therefore owns a durable write lease:

```text
active_writer = NONE | CHATGPT | CODEX
```

Typical handoff:

```text
CHATGPT
  -> checkpoint / diff / validation snapshot
  -> release write lease
  -> CODEX
  -> observe / steer / respond / interrupt
  -> checkpoint / handback
  -> CHATGPT
```

Reads and observation may continue while Codex is the writer. Mutations fail closed when writer ownership is stale or ambiguous.

## Revised backend architecture

The bridge must no longer hard-code one infrastructure stack into Supervisor Core.

```text
                         User
                          |
                          v
                    ChatGPT Web
                          |
                          v
                Supervisor MCP Surface
                          |
                          v
+----------------------------------------------------+
|              Codex Supervisor Bridge              |
|                                                    |
|  Supervisor Core                                   |
|  - Memory / Context Pack                           |
|  - Intent / Plan / Revision                        |
|  - Constraints / Decisions                         |
|  - Execution Mode / Write Lease                    |
|  - Checkpoints / Drift Review                      |
|  - Handoff / Handback                              |
|  - Hard Replan                                     |
+--------------------------+-------------------------+
                           |
                    Backend interfaces
                           |
          +----------------+----------------+
          |                |                |
          v                v                v
   WorkspaceBackend    AgentBackend    DeliveryBackend
```

Supervisor Core should depend on backend-neutral capabilities, not product names.

### WorkspaceBackend

Target semantic surface:

- open/reuse supervised workspace;
- create/reuse managed worktree;
- read/search files;
- apply constrained edits;
- run bounded workspace commands and command sessions;
- inspect Git state;
- aggregate/show changes;
- close/release workspace.

### AgentBackend

Target semantic surface:

- start read-only plan;
- inspect plan/runtime status;
- start approved implementation;
- observe bounded progress;
- steer the current active turn;
- interrupt;
- list pending approvals/questions;
- respond to pending interactions;
- resume/recover runtime identity.

### DeliveryBackend

Target semantic surface:

- code review;
- QA;
- commit/push;
- create/update draft PR;
- read CI status;
- retrieve bounded failure detail;
- request/perform CI fix iteration.

## Backend profiles under evaluation

### Profile A — existing full workflow backend

Already implemented through earlier phases:

```text
Workspace / workflow / delivery: Kandev
Agent runtime: codex-control-plane-mcp
Git facts: GitHub
```

This remains a valid fallback and provides a mature Feature Dev workflow including review, QA, PR, and CI-fixup stages.

### Profile B — lightweight personal backend

The preferred candidate for evaluation after P6:

```text
Direct workspace / worktree / review: DevSpace
Live Codex control: Local-Codex-Bridge
Delivery facts / PR / CI: GitHub
```

Important rules:

- do **not** expose DevSpace directly to ChatGPT in production if that bypasses Supervisor mode, revision, write-lease, or event controls;
- proxy only the required DevSpace semantics through Supervisor Bridge;
- do **not** replace Supervisor task memory with DevSpace agent/session persistence;
- do **not** replace Supervisor checkpoints with Local-Codex-Bridge's thread-keyed checkpoint;
- do **not** fork DevSpace merely to obtain steer/interrupt unless an adapter-based proof shows that forking is necessary;
- prefer Local-Codex-Bridge's existing `observe / steer / respond / interrupt` semantics over reimplementing fragile Codex app-server edge cases from scratch.

## Why DevSpace is being evaluated

DevSpace already provides useful general-purpose infrastructure that we should avoid rebuilding unnecessarily:

- ChatGPT-oriented Remote MCP infrastructure;
- local workspace identity;
- managed Git worktrees;
- project instruction loading (`AGENTS.md`, `CLAUDE.md`);
- Skills;
- file editing and command sessions;
- Git-backed `show_changes` / review references;
- local subagent manager / daemon / SQLite persistence;
- Windows support;
- direct Codex app-server provider support.

Its current Codex provider is **not** by itself a complete replacement for our live-control requirements: current code centers on `thread/start|resume -> turn/start -> turn/completed`, uses `approvalPolicy: never`, and rejects active app-server server requests. Treat live steer/interrupt/approval as a separate AgentBackend concern until proven otherwise.

## Why Local-Codex-Bridge is being evaluated

It already implements the difficult live-runtime semantics required by the product:

- persistent Codex turn start/resume;
- bounded observe;
- same-turn `turn/steer` with expected turn ID;
- pending approval / user-input forwarding;
- `turn/interrupt`;
- defensive handling of mutating-request acknowledgement timeouts where the outcome is UNKNOWN;
- Windows-oriented local operation.

Its task checkpoint is not a replacement for Supervisor Core because it is keyed to a native Codex thread rather than the permanent supervised task.

## Current implementation status

### Completed and merged

- **P1 — Persistent Memory Core**
- **P2 — ChatGPT Remote MCP Surface**
- **P3 — Kandev Adapter**
- **P4 — Codex Live Control / Plan Gate / Steer / Interrupt**
- **P5 — Structured Checkpoints / Supervisor Review**
- **P6 — Human hard override and hard replan**
  - Work Snapshot;
  - intent version bump;
  - old plan supersession;
  - Codex interrupt confirmation;
  - KEEP / MODIFY / DROP classification;
  - read-only replan transition;
  - stale-revision protection.

### P6.5 — code-complete status

P1 through P6 are complete. P6.5 code is complete and was reviewed in Draft
PR #8. The implementation is covered by fake/protocol tests; this statement is
intended to remain correct after the change is merged into `main`.

The following behaviors are implemented and covered:

- task-scoped `DIRECT` / `HYBRID` / `CODEX_SUPERVISED` modes;
- `MANUAL_ONLY` / `SUPERVISOR_ALLOWED` delegation policy;
- durable `active_writer` + `writer_epoch` single-writer fencing;
- revision/epoch-fenced handoff and handback state;
- two-phase direct mutation (`PREPARED` -> external change -> evidence -> final);
- `RECONCILIATION_REQUIRED` fail-closed behavior;
- semantic Direct Workspace MCP tools backed by `WorkspaceBackend` and DevSpace;
- Local-Codex-Bridge `AgentBackend` with same-turn steer, observe, interrupt,
  pending interaction normalization, and UNKNOWN acknowledgement semantics;
- backend-neutral external-call race compensation for plan/execution start,
  with a durable `COMPENSATION_REQUIRED / INTERRUPT_PENDING` latch written
  before interrupt, and guards that fail closed on UNKNOWN or failed
  interrupt compensation;
- backend-neutral Supervisor Plan Gate carrying a read-only `PlanResult` through
  local DRAFT/import, explicit APPROVED review, writer-lease validation, and
  workspace-write execution;
- one fake/protocol orchestration test covering the same TaskMemory semantics
  under Profile A and Profile B agent implementations;
- existing Control Plane wrapped by the same backend-neutral AgentSnapshot model;
- CheckpointService consumption of normalized AgentSnapshot data;
- automatic capability resolution with user-facing `READY` / `DEGRADED` /
  `UNAVAILABLE` summaries that hide backend jargon;
- Context Pack preservation of execution state, workspace identity, Git/review
  evidence, checkpoints, and reconciliation warnings;
- SQLite persistence/recovery for task, execution, workspace, direct operation,
  handoff, review, checkpoint, and agent compensation state (schema version 7).

P6.5 deliberately does **not** claim real Windows installation, Local-Codex-
Bridge process startup, DevSpace OAuth, ChatGPT Remote MCP tunnel proof, or
real Profile A/B runtime validation. Those are P6.6 integration gates.

### P6.5 — merged

PR #8 was merged into `main` on 2026-08-27 with merge commit
`5fa3ca7a6d2ce91c5a55629f9882945e908963c2`. The merge preserved the P6.5
history and schema version 7. The P6.6 implementation branch is
`feat/p6-6-windows-bootstrap`.

### P6.6 — active

P6.6 is the active phase: Windows Integration / Bootstrap / Real A-B Proof.
The first implementation slice is intentionally backend-neutral and layered:

- persistent versioned user configuration with Basic / Advanced projections;
- Windows-aware application data directories under `%LOCALAPPDATA%` with
  portable test overrides;
- platform, executable, runtime, project, GitHub, port, and local bind probes;
- bounded process lifecycle management with stale PID/lock recovery;
- automatic loopback port allocation;
- structured Doctor and Repair models suitable for CLI and future GUI use;
- secret storage and secure remote access abstractions with redaction tests;
- fake process/provider harnesses for Profile A/B semantic comparison.

The current code-level work does not claim real Windows OAuth, ChatGPT Web
Remote MCP attachment, tunnel-provider login, or authenticated Codex runtime
proof. Those remain opt-in/manual gates after the fake and protocol coverage is
complete. Profile A remains the fallback until Profile B passes the documented
real Windows scenario.

### Revised next phases

#### P6.5 — Execution Modes + Backend Abstraction (code complete; PR #8)

- task-scoped DIRECT / HYBRID / CODEX_SUPERVISED mode;
- MANUAL_ONLY / SUPERVISOR_ALLOWED delegation policy;
- durable single-writer lease;
- handoff / handback snapshot;
- WorkspaceBackend / AgentBackend / DeliveryBackend protocols;
- wrap existing Kandev and Control Plane implementations as Profile A;
- DevSpace direct-workspace adapter;
- Local-Codex-Bridge agent adapter;
- backend-neutral AgentSnapshot consumed by checkpoints;
- capability/configuration resolver;
- low-learning-cost / zero-config normal workflow principle.

**Next active phase: P6.6 — Windows integration A/B proof.**

#### P6.6 — Windows integration A/B proof

Run the same supervised scenario against Profile A and Profile B. Required gates include:

- direct ChatGPT edit + validation;
- direct -> Codex handoff;
- real active-turn steer;
- approval/user-input forwarding;
- interrupt + hard replan;
- Codex -> ChatGPT handback;
- process restart / task resume;
- new ChatGPT conversation resume;
- worktree isolation;
- diff/review evidence;
- failure/recovery behavior.

Profile B becomes the default only if it passes these gates. Existing Profile A remains a fallback until the lightweight path is proven.

#### P7 — Backend-neutral Review / QA / PR / CI loop

Do not write P7 as Kandev-only. Delivery orchestration must consume the DeliveryBackend contract.

#### P8 — Automated supervised loop

Automate recurring checkpoint collection, supervisor decisions, steer/replan/fix cycles, and final acceptance while preserving user interruption at any time.

## Configuration and capability resolver

P6.5 introduces an automatic resolver that detects available local capabilities
and chooses/repairs backend components without requiring normal users to select
backends manually.

Expected responsibilities:

- detect Git and repository state;
- detect Codex and supported app-server features;
- detect/install/locate local workspace backend where supported;
- detect AgentBackend health;
- detect GitHub connection;
- allocate non-conflicting local ports internally;
- start/restart local daemons/workers;
- surface a compact health summary;
- provide an `automatic repair` path for common failures;
- keep advanced configuration available but hidden from the normal workflow.

The number of internal components must **not** determine the number of concepts the user has to learn.

## Safety and control principles

1. Latest explicit user override outranks every plan and agent-local instruction.
2. Active HARD constraints are never silently dropped from Context Packs.
3. A stale `expected_revision` cannot mutate current supervised state.
4. Plan approval is explicit before Codex implementation in supervised execution.
5. Direct and Codex writers do not share a worktree write lease concurrently.
6. Dangerous or ambiguous runtime states fail closed.
7. Raw reasoning streams are not required or stored as supervisor memory.
8. Historical decisions remain searchable but superseded decisions are not current instructions.
9. Browser conversation history is an interface, not project memory.
10. Backend implementation details are hidden from normal product UX.

## Resume instructions for a new ChatGPT conversation

When a new conversation needs to continue development of this repository:

1. inspect current `main` and open feature branches/PRs;
2. read this file;
3. read `docs/P6.5-execution-modes.md`;
4. read the phase-specific document for the active branch;
5. preserve Supervisor Core and the revised backend-neutral direction;
6. do not revert to a fixed `Kandev + Control Plane` architecture without new evidence;
7. do not delete the existing backend integrations until Profile B has passed real Windows A/B gates;
8. continue implementation from the repository's actual CI/branch state rather than from old chat text.
