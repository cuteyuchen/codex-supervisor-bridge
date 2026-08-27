# P1 — Persistent Memory Core

## Goal

P1 makes supervised development independent from any single ChatGPT conversation.
The browser conversation is an operator interface; durable project truth lives in the bridge.

A fresh ChatGPT conversation must be able to resume a task from the database without depending on hidden or previous chat context.

## Ownership boundaries

The bridge stores supervisory state only.

- **Supervisor Bridge** owns user intent, constraints, decisions, revisions, context snapshots, and supervisory history.
- **Kandev** will own development workflow, worktrees, and workflow-stage state once P3 is connected.
- **Codex Control Plane** will own Codex thread/turn/runtime facts once P4 is connected.
- **GitHub** remains the source of truth for commits, pull requests, and CI facts.

The bridge stores references and summaries of external evidence; it must not silently become a second Git or Codex transcript database.

## Version model

Three counters are intentionally independent.

### `intent_version`

Changes when the user's effective goal changes materially.

Example: multi-save becomes single-save.

### `plan_version`

Changes when a new implementation plan is created.

A plan can be `DRAFT`, `APPROVED`, `REJECTED`, or `SUPERSEDED`.
Only an approved plan is authoritative for ordinary implementation contexts.

### `revision`

Monotonically advances for supervisor-relevant mutable state.
Every future mutating MCP action must supply the revision it was based on. If it is stale, the bridge rejects the mutation with `STALE_CONTEXT`.

Derived records such as summaries, evidence references, and Context Pack snapshots do **not** advance task revision. Otherwise merely reading or summarizing a task would invalidate the supervisor's current decision.

## Durable data

SQLite v1 contains:

- `supervised_tasks`
- `task_events`
- `task_decisions`
- `task_constraints`
- `task_plans`
- `task_summaries`
- `evidence_index`
- `context_snapshots`
- `memory_documents`
- optional `memory_fts` (FTS5)

Schema changes are forward-only and versioned through `schema_meta`.

## Immutable event log

`task_events` is append-only. Historical events are never rewritten into a new meaning.

Actors are explicit:

- `USER`
- `SUPERVISOR`
- `CODEX`
- `KANDEV`
- `GITHUB`
- `SYSTEM`

Historical events are evidence, not automatically current instructions.
For example, a superseded decision remains searchable in history but its old body is not reintroduced into the default Context Pack.

## Decision registry

Decisions have lifecycle state:

- `ACTIVE`
- `SUPERSEDED`
- `REVOKED`

Only `ACTIVE` decisions are injected as current truth.
Superseded decisions remain searchable so the system can answer questions such as "why did this change?" without allowing obsolete direction to steer current work.

## Constraint registry

Constraints have severity:

- `HARD`
- `SOFT`
- `PREFERENCE`

Active `HARD` constraints are mandatory Context Pack content. They are never dropped simply to fit a token budget.
If mandatory content itself exceeds the configured hard budget, the Context Pack reports that fact instead of silently deleting hard constraints.

## Context Packs

`ContextPackBuilder` produces deterministic progressive-disclosure context.

Default character budget:

- target: 48,000 characters
- hard maximum for optional material: 64,000 characters

The token count is currently a conservative dependency-free estimate (`ceil(chars / 4)`). The memory layer intentionally does not bind itself to one model tokenizer.

### Mandatory sections

1. Task identity and versions
2. Current user goal
3. Active hard constraints
4. Active decisions
5. Applicable approved/review plan
6. Current state

Mandatory sections are never removed by budget trimming.

### Optional sections

1. Soft/preference constraints
2. Latest Codex checkpoint/progress
3. Recent user overrides
4. Recent supervisor decisions
5. Runtime/Git references
6. Evidence references
7. Current decision prompt

Optional sections are progressively trimmed or omitted when the target/hard budget is reached.

### Modes

- `resume`
- `checkpoint_review`
- `plan_review`
- `final_review`
- `debug`

The mode changes what plan/status evidence is emphasized and what decision the supervisor is expected to make.

## Evidence and search

The bridge stores compact evidence references instead of injecting raw logs and large diffs into every prompt.

Search uses SQLite FTS5 when available and falls back to `LIKE` when FTS5 is unavailable.
Historical records, including superseded decisions/plans, remain searchable with their lifecycle status attached.

Future MCP tools can therefore use progressive disclosure:

1. get a bounded Context Pack;
2. search memory when more history is needed;
3. retrieve exact evidence only when the supervisor requests it.

## Transaction and stale-context guarantees

Supervisor-relevant mutations run inside `BEGIN IMMEDIATE` SQLite transactions.

The mutation sequence is:

1. read current task revision;
2. compare it with `expected_revision`;
3. update durable state and advance revision;
4. append the corresponding event;
5. commit atomically.

A stale caller receives `StaleRevisionError` / `STALE_CONTEXT` and no partial mutation is committed.

This prevents an old ChatGPT review from restarting or steering a task after a newer user override has already changed the canonical state.

## Current public application API

`MemoryService` is the application-facing facade. Future MCP handlers should depend on it rather than issuing SQLite queries directly.

Current capabilities include:

- create a supervised task;
- resume a task from durable memory;
- build a Context Pack;
- search task memory;
- assert a task revision.

`MemoryStore` additionally exposes typed domain operations for plans, decisions, constraints, events, summaries, and evidence.

## P1 acceptance criteria

P1 is complete when automated tests prove all of the following:

- file-backed state survives process restart;
- stale revision mutations are rejected atomically;
- hard constraints survive Context Pack budget pressure;
- superseded decisions do not return as current context;
- historical/superseded records remain searchable;
- plan draft/approve/supersede semantics are durable;
- derived summaries/evidence/snapshots do not invalidate supervisor revision;
- a new process can resume a task without previous chat history;
- Python 3.12 and 3.13 CI pass lint and tests.

## P1 non-goals

P1 intentionally does not yet implement:

- ChatGPT Remote MCP transport (P2)
- Kandev integration (P3)
- Codex app-server / live steering (P4)
- automated checkpoint loops (P5+)
- multi-user authentication / RBAC
- PostgreSQL / Redis / distributed workers
- vector databases
- automatic merge
