# Codex Supervisor Bridge

A persistent supervision, memory, steering, and workflow bridge for ChatGPT and Codex.

This repository is the control layer between ChatGPT Web, Kandev, Codex Control Plane, and GitHub. Its core goal is to make long-running AI-assisted development resumable, auditable, steerable, and safe across ChatGPT conversation boundaries.

## Status

Early development. The first milestone is the persistent memory core and deterministic Context Pack builder.

## Design principles

- ChatGPT conversations are an interface, not the source of project memory.
- User intent and active hard constraints outrank plans and agent-local decisions.
- All important task events are append-only and auditable.
- Current intent, plan, and task revisions are explicit and independently versioned.
- Mutating supervisor actions must be protected against stale context.
- Raw evidence stays retrievable without being injected into every model context.
- Kandev owns development workflow/worktrees; Codex Control Plane owns Codex runtime state; GitHub owns code/PR/CI facts.

## Planned milestones

1. P1 — Memory Core + Context Pack Builder
2. P2 — Remote MCP surface for ChatGPT
3. P3 — Kandev adapter
4. P4 — Codex live control and steering
5. P5 — Checkpoints and supervisor review
6. P6 — Human override and hard replan
7. P7 — Review / QA / PR / CI integration
8. P8 — Automated supervised loop
