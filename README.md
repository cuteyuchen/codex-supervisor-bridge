# Codex Supervisor Bridge

A persistent supervision, memory, steering, and workflow bridge for ChatGPT and Codex.

Codex Supervisor Bridge sits between ChatGPT Web, Kandev, Codex Control Plane, and GitHub. Its goal is to make long-running AI-assisted development resumable, auditable, steerable, and safe across ChatGPT conversation boundaries.

## Why it exists

A long ChatGPT conversation should not be the project's memory, and a long Codex turn should not become a black box.

The bridge keeps durable supervisor state outside the browser chat:

- current user intent;
- active hard constraints;
- approved decisions and plans;
- append-only supervisory events;
- explicit intent / plan / task revisions;
- bounded Context Packs for cross-conversation recovery;
- searchable historical evidence without re-injecting obsolete decisions as current truth;
- durable Codex workflow / operation / thread / turn identity for restart-safe supervision;
- bounded structured checkpoints instead of injecting raw Codex progress streams into every ChatGPT context.

A new ChatGPT conversation can therefore resume a task from canonical external memory instead of depending on the old browser transcript.

## Architecture

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
  |-- Revision Guard / Plan Gate
  |-- Checkpoint Aggregation / Review
  |-- Override / Hard Replan (later phase)
  |
  +-------------------+
  |                   |
  v                   v
Kandev             Codex Control Plane
  |                   |
  |                   +-- central worker
  |                   +-- Codex app-server
  |                   +-- turn/start / steer / interrupt
  |
  +-- workflow / worktree / review / PR / CI
             |
             v
           GitHub
```

The bridge is intentionally not a second IDE and does not expose an arbitrary shell to ChatGPT.

## Current status

### P1 — Persistent Memory Core ✅

Implemented and merged:

- SQLite persistence and forward schema migrations;
- optimistic `expected_revision` locking with `STALE_CONTEXT`;
- append-only events;
- decision / constraint / plan lifecycle state;
- evidence and summary indexing;
- SQLite FTS5 search with fallback;
- deterministic bounded Context Packs;
- process-restart / cross-conversation recovery tests.

### P2 — ChatGPT MCP Surface ✅

Implemented and merged:

- official MCP Python SDK v2;
- semantic supervisor tools only;
- Streamable HTTP endpoint on loopback by default;
- structured task/context/search/plan responses;
- safe model-readable stale-revision and domain errors;
- in-process MCP protocol tests;
- cross-process task resume without previous ChatGPT history.

Real ChatGPT Web + Secure MCP Tunnel connectivity is intentionally deferred until the local integration boundary.

### P3 — Kandev Adapter ✅

Implemented and merged:

- typed client for Kandev external MCP;
- runtime capability discovery;
- revision-safe Supervisor Task ↔ Kandev Task binding;
- stable Kandev `external_id` for idempotent provisioning;
- Kandev session/conversation observation through Supervisor MCP;
- safe integration error boundary;
- provisioning hard gate: `start_agent=false`, `autopilot=false`.

### P4 — Codex Live Control ✅

Implemented and merged:

- contract-verified `codex-control-plane-mcp` adapter;
- schema v2 durable Codex workflow/operation/thread/turn state;
- read-only Codex Plan Mode;
- local DRAFT import and explicit Supervisor plan approval;
- remote-plan drift guard before execution;
- fixed `workspace-write` execution surface;
- current-turn soft steering through upstream `steer_turn`;
- pending approval/question handling;
- interrupt-to-PAUSED control path;
- fail-closed compensation when a user revision races an in-flight remote Codex write;
- raw `codex_submit_task` / sandbox escalation intentionally hidden from ChatGPT.

### P5 — Checkpoints and Supervisor Review 🚧

Under development:

- schema v3 durable checkpoint/review records;
- deterministic HEARTBEAT / PROGRESS / GATE classification;
- filtering of high-frequency reasoning/token/text deltas;
- source-fingerprint deduplication;
- structured completion / file / validation / blocker / risk / next-step fields;
- latest checkpoint rendered into Context Packs instead of raw Codex progress events;
- optimistic-revision checkpoint review with CONTINUE / STEER / INTERRUPT / REPLAN / ACCEPT;
- explicit follow-up control actions rather than hidden automatic steering.

See `docs/P1-memory-core.md`, `docs/P2-remote-mcp.md`, `docs/P3-kandev-adapter.md`, `docs/P4-codex-live-control.md`, `docs/P5-checkpoints.md`, and `docs/architecture.md`.

## Local server targets

Supervisor MCP:

```text
http://127.0.0.1:8765/mcp
```

Kandev external MCP default:

```text
http://127.0.0.1:38429/mcp
```

Codex Control Plane is invoked by the Bridge as a client-mode MCP gateway (default executable `codex-control-plane-mcp`) and is expected to share state with a separately running central worker. The worker, not the Bridge process, owns the long-running Codex app-server runtime.

All local control services are expected to remain local. Supervisor MCP will later be exposed to ChatGPT through an authenticated Secure MCP Tunnel rather than opening the local service directly to the public internet.

## Design principles

- ChatGPT conversations are an interface, not the source of project memory.
- Latest user intent and active HARD constraints outrank plans and agent-local decisions.
- Important task events are append-only and auditable.
- Intent, plan, and task revisions are explicit and independently versioned.
- Mutating supervisor actions are protected against stale context.
- Superseded records remain searchable history but are not current instructions.
- Raw evidence is retrieved progressively instead of injected into every model context.
- Kandev owns development workflow/worktrees; Codex Control Plane owns Codex runtime state; GitHub owns code/PR/CI facts.
- The ChatGPT-facing MCP surface exposes semantic operations, not unrestricted local execution.
- Kandev provisioning alone never starts an agent.
- Codex execution is plan-gated: read-only plan → local approval → remote equality check → workspace-write.
- Soft steering modifies the current active turn; architecture/scope changes require interrupt + replan.
- Checkpoints summarize high-signal runtime facts; raw reasoning/token deltas are not supervisory context.
- Heartbeats do not churn task revision; meaningful progress/gates do.

## Planned milestones

1. P1 — Memory Core + Context Pack Builder ✅
2. P2 — Remote MCP surface for ChatGPT ✅
3. P3 — Kandev adapter ✅
4. P4 — Codex live control and steering ✅
5. P5 — Checkpoints and supervisor review 🚧
6. P6 — Human override and hard replan
7. P7 — Review / QA / PR / CI integration
8. P8 — Automated supervised loop
