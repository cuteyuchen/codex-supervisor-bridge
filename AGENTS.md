# Agent Instructions

Before making architectural or cross-cutting changes in this repository:

1. Read `docs/PROJECT-STATE.md` first. It is the current product and architecture baseline.
2. Read `docs/P6.5-execution-modes.md` before changing execution, workspace, Codex, or backend behavior.
3. Inspect the actual current branch, PR, and CI status before assuming a milestone is complete.
4. Preserve Supervisor Core as the canonical source of task intent, constraints, decisions, revisions, checkpoints, hard replans, and cross-conversation context.
5. Treat Kandev and `codex-control-plane-mcp` as existing backend implementations, not permanent hard-coded architecture.
6. Do not remove the existing backend path until the DevSpace + Local-Codex-Bridge profile passes the documented Windows A/B gates.
7. Keep the normal user workflow low-learning-cost and mostly automatic; backend names, ports, SQLite paths, workers, thread IDs, and similar details belong in diagnostics/advanced settings.
8. Enforce the single-writer invariant for a supervised worktree: ChatGPT direct coding and Codex do not mutate the same worktree concurrently by default.
9. Prefer semantic, bounded MCP operations over unrestricted raw shell/filesystem access.
10. Do not auto-merge or make destructive Git changes unless explicitly authorized.
