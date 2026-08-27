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
- searchable historical evidence without re-injecting obsolete decisions as current truth.

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
  |-- Revision Guard
  |-- Checkpoint / Override policy (later phases)
  |
  +-------------------+
  |                   |
  v                   v
Kandev             Codex Control Plane
  |                   |
  |                   +-- Codex app-server / turn steering
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

### P2 — ChatGPT MCP Surface 🚧

Under development:

- official MCP Python SDK v2;
- semantic supervisor tools only;
- Streamable HTTP endpoint on loopback by default;
- structured task/context/search/plan responses;
- model-readable stale-revision and domain errors;
- in-process protocol tests before real ChatGPT/tunnel integration.

See `docs/P1-memory-core.md`, `docs/P2-remote-mcp.md`, and `docs/architecture.md`.

## Local server target

Once P2 is complete, the default local endpoint will be:

```text
http://127.0.0.1:8765/mcp
```

The server is intended to remain bound to localhost and be exposed to ChatGPT through an authenticated Secure MCP Tunnel rather than directly to the public internet.

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

## Planned milestones

1. P1 — Memory Core + Context Pack Builder ✅
2. P2 — Remote MCP surface for ChatGPT 🚧
3. P3 — Kandev adapter
4. P4 — Codex live control and steering
5. P5 — Checkpoints and supervisor review
6. P6 — Human override and hard replan
7. P7 — Review / QA / PR / CI integration
8. P8 — Automated supervised loop
