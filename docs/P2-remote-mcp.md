# P2 — ChatGPT Remote MCP Surface

## Goal

Expose the durable P1 supervisor memory to ChatGPT through a small semantic MCP API while keeping local execution capabilities behind the bridge.

P2 deliberately does **not** expose an arbitrary shell, process launcher, raw SQL, or unrestricted filesystem access.

## SDK and transport

P2 targets the official MCP Python SDK v2 (`mcp>=2,<3`) and uses `MCPServer`.

The default runtime transport is Streamable HTTP:

```text
http://127.0.0.1:8765/mcp
```

The server binds to loopback by default. It is not intended to be opened directly to the public internet. A later local integration step will place an authenticated Secure MCP Tunnel in front of this endpoint for ChatGPT Web.

A stdio transport is also available for diagnostics and compatible local hosts.

## Running locally

After installing the package:

```text
python -m codex_supervisor_bridge
```

or:

```text
codex-supervisor-bridge
```

Equivalent explicit command:

```text
codex-supervisor-bridge \
  --database ~/.codex-supervisor-bridge/supervisor.db \
  --transport streamable-http \
  --host 127.0.0.1 \
  --port 8765 \
  --mcp-path /mcp
```

Environment variables:

- `SUPERVISOR_DB_PATH`
- `SUPERVISOR_HOST`
- `SUPERVISOR_PORT`

## MCP instruction contract

The server tells the supervising model:

1. browser chat history is not canonical project memory;
2. read the latest task/context before a mutation;
3. pass the returned `revision` as `expected_revision`;
4. on `STALE_CONTEXT`, re-read current state instead of retrying an old decision;
5. ACTIVE records are current truth while SUPERSEDED records are historical evidence;
6. HARD constraints outrank plans and agent-local choices.

## Tool annotations

Read operations are marked `read_only_hint=True` and `open_world_hint=False`.

Supervisor mutations are marked non-read-only, non-idempotent, and non-destructive. A supersede operation changes current truth but preserves historical records instead of deleting them.

`resume_supervised_task` is intentionally read-only. Merely reopening a task must not mutate the database or invalidate the revision it just returned.

## P2 tool surface

### Task and context

- `create_supervised_task`
- `get_supervised_task`
- `resume_supervised_task`
- `get_context_pack`
- `get_task_timeline`
- `search_task_memory`

### User intent and constraints

- `record_user_override`
- `update_task_intent`
- `add_task_constraint`
- `supersede_task_constraint`

### Supervisor decisions

- `add_task_decision`
- `supersede_task_decision`

### Plan gate

- `create_task_plan`
- `get_current_plan`
- `approve_task_plan`
- `reject_task_plan`

P2 does not yet include Codex runtime control. `continue`, `steer`, `interrupt`, and `replan` will become real control operations only after the Codex Control Plane adapter is connected.

## Structured responses

Mutating tools return the changed object together with the latest `TaskMemory`. This is deliberate: the caller immediately receives the new canonical revision that must be used for the next mutation.

Example shape:

```json
{
  "task": {
    "task_id": "GAME-301",
    "revision": 14,
    "intent_version": 2,
    "plan_version": 4
  },
  "decision": {
    "decision_id": "dec_...",
    "status": "ACTIVE"
  }
}
```

## Error behavior

Domain exceptions are surfaced as MCP tool errors that the supervising model can read and react to.

A stale mutation is expected to look conceptually like:

```text
STALE_CONTEXT task=GAME-301 expected_revision=12 current_revision=14
```

The model must then call `get_supervised_task` or `get_context_pack` again before issuing another mutation.

An unknown task is likewise a tool error, not a successful result containing an error-looking string.

## Testing strategy

The official SDK can connect directly to an `MCPServer` in process, so P2 protocol/tool tests do not need an HTTP port or local tunnel.

Automated tests cover:

- create/read/resume through MCP;
- hard constraints in restored Context Packs;
- read-only resume not advancing revision;
- mutation returning the latest revision;
- stale revision becoming a model-readable tool error;
- unknown task becoming a tool error;
- superseded decisions remaining searchable but absent from current context;
- plan create/approve gate through MCP;
- database close/reopen and a fresh MCP server resuming the same task;
- loopback Streamable HTTP defaults and configuration validation.

## P2 acceptance criteria

P2 is complete when:

1. the official MCP SDK v2 installs on supported Python versions;
2. all P1 tests remain green;
3. in-process MCP client tests pass on Python 3.12 and 3.13;
4. tool failures are correctly distinguished from successful structured results;
5. read-only task/context tools do not change task revision;
6. no MCP tool provides arbitrary command execution or unrestricted local access;
7. the server can be launched locally with Streamable HTTP on `127.0.0.1`.

Actual ChatGPT Web + Secure MCP Tunnel connectivity is an integration test outside GitHub CI and will be performed only when the code-level P2 contract is green.
