# P3 — Kandev Adapter

## Goal

P3 connects Supervisor Bridge to Kandev's external MCP so a canonical supervisor task can be represented in Kandev and its sessions/conversation can be inspected without giving Kandev ownership of user intent.

P3 is intentionally a preparation and observation layer. It does **not** start Codex or any other agent.

## Ownership boundary

Supervisor Bridge remains authoritative for:

- user goal and `intent_version`;
- active constraints;
- supervisor decisions;
- plan/revision safety;
- cross-system binding identity.

Kandev remains authoritative for:

- its task/workflow identifiers;
- workflow steps;
- worktree/task execution environment;
- Kandev session/conversation facts;
- later Review / QA / PR / CI workflow stages.

The bridge persists only the Kandev task ID and compact supervisory evidence needed to correlate both systems.

## External MCP endpoint

Default:

```text
http://127.0.0.1:38429/mcp
```

Override with:

```text
KANDEV_MCP_URL
```

The adapter is lazy. Starting Supervisor Bridge does not require Kandev to be online; a Kandev connection is opened only when a Kandev tool is called.

## Required Kandev tools

The P3 compatibility probe expects:

- `list_workspaces_kandev`
- `list_workflows_kandev`
- `list_workflow_steps_kandev`
- `list_repositories_kandev`
- `list_tasks_kandev`
- `create_task_kandev`
- `list_task_sessions_kandev`
- `get_task_conversation_kandev`
- `move_task_kandev`
- `update_task_state_kandev`

Only a safe subset is currently exposed to ChatGPT. `move_task_kandev` and `update_task_state_kandev` remain internal until the supervisor state machine owns those transitions.

## Idempotent task provisioning

Each Supervisor task maps to a stable Kandev external ID:

```text
codex-supervisor-bridge:<supervisor_task_id>
```

Kandev's `external_id` idempotency behavior allows a create request to be safely retried after a partial cross-system failure.

Provisioning sequence:

```text
read Supervisor task at expected_revision
        |
        v
build Kandev create request
        |
        | external_id = codex-supervisor-bridge:<task_id>
        | start_agent = false
        | autopilot = false
        v
create/reuse Kandev task
        |
        v
extract Kandev task ID
        |
        v
bind into Supervisor memory using the SAME expected_revision
```

If a user override changes the Supervisor task while the remote call is in flight, the local bind fails with `STALE_CONTEXT`. Retrying with the latest revision reuses the same Kandev task through the stable `external_id` rather than creating a duplicate.

## No agent startup in P3

P3 always forces:

```text
start_agent = false
autopilot = false
```

This is a hard phase boundary.

The reason is not that Kandev cannot launch agents. The reason is that Codex must not begin a long black-box implementation before P4 connects the plan gate, progress state, native `turn/steer`, interruption, approval, and recovery surfaces.

## ChatGPT-facing P3 tools

When a Kandev coordinator is configured, Supervisor MCP adds exactly four tools:

### `get_kandev_capabilities`

Read-only. Lists the discovered external MCP tools and reports missing P3 requirements.

### `provision_kandev_task`

Revision-protected mutation. Creates/reuses a Kandev task, never starts an agent, and binds the resulting Kandev task ID into Supervisor memory.

### `get_kandev_sessions`

Read-only. Reads sessions from the bound Kandev task.

### `get_kandev_conversation`

Read-only. Reads the bound Kandev task conversation for supervisor inspection.

Raw workflow movement and agent lifecycle controls remain unavailable to ChatGPT in P3.

## Adapter response compatibility

Kandev's current Go external MCP returns backend payloads as JSON text content.

`KandevAdapter` therefore parses JSON text. It also accepts MCP structured content so the Bridge does not depend on a single response representation if Kandev later adopts structured output.

Non-JSON text where a JSON object is required is treated as a protocol error rather than guessed or parsed heuristically.

## Error boundary

Expected integration failures are model-readable:

- Kandev unavailable;
- required Kandev tool missing;
- Kandev MCP tool returned an error;
- Kandev response shape is incompatible;
- Supervisor revision became stale;
- attempted rebind to a different Kandev task.

Unexpected implementation/network details remain redacted by the outer MCP server.

## P3 acceptance criteria

P3 is complete when automated tests prove:

1. the adapter discovers the required Kandev tool surface;
2. current JSON text responses are parsed correctly;
3. structured MCP responses are also accepted;
4. remote MCP tool errors become typed integration errors;
5. provisioning forces `start_agent=false` and `autopilot=false`;
6. provisioning uses the stable Supervisor-derived `external_id`;
7. successful binding advances Supervisor revision exactly once;
8. provisioning replay after a successful bind is a no-op;
9. Kandev sessions and conversation are readable through Supervisor MCP;
10. the Supervisor MCP has 16 tools without Kandev and 20 tools with P3 configured;
11. no P3 ChatGPT-facing tool can move workflow state or start an agent;
12. Python 3.12 and 3.13 CI remain green.

Real local Kandev connectivity is an integration test after the code-level contract is green. P3 itself should not force the user to run Kandev merely to validate adapter logic in CI.
