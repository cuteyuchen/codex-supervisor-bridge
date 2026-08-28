# Project State and Current Architecture Baseline

> **Read this first when resuming the project in a new ChatGPT conversation.**
>
> This document records the current product intent and the architecture decisions that supersede earlier fixed-backend assumptions. It is deliberately more durable than a browser conversation.

Last updated: 2026-08-28

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
- `codex-supervisor configure` persists project directory and Basic user intent
  (development style, command policy, delegation, auto commit/PR), plus the
  Local-Codex-Bridge repository and Node executable, without exposing backend
  names in the normal workflow;
- Windows-aware application data directories under `%LOCALAPPDATA%` with
  portable test overrides;
- platform, executable, runtime, project, GitHub, port, and local bind probes;
- bounded process lifecycle management with stale PID/lock recovery;
- optional process readiness probes with bounded startup timeout and graceful
  timeout cleanup;
- automatic loopback port allocation;
- structured Doctor and Repair models suitable for CLI and future GUI use;
- secret storage and secure remote access abstractions with redaction tests;
- Codex readiness fields for executable/process/version/auth/workspace checks;
- fail-closed runtime recovery for orphaned Codex writer ownership;
- controlled Direct Workspace command authorization with bounded sessions and
  command evidence;
- durable Direct Workspace command-session identity/status with restart resume
  and UNKNOWN-outcome reconciliation;
- fail-closed ambiguous process-PID handling and loopback-only local MCP HTTP
  binding;
- user-facing bootstrap projections hide internal repair/provider operation
  names while advanced diagnostics retain them;
- DevSpace released-contract compatibility for npm versions 1.0.5 and 1.0.8:
  one Bridge-managed flat `config.json`, one managed `auth.json`, no default
  `~/.devspace` source, and no stale managed `config.jsonc`;
- a local-only Supervisor -> DevSpace OAuth driver using the MCP Python SDK
  OAuth client (discovery, dynamic registration, PKCE, bearer injection, and
  refresh) with SecretStore-backed owner/OAuth credentials, redacted
  diagnostics, and a fake loopback OAuth protocol test that also proves
  restart reuse of persisted tokens;
- Local-Codex-Bridge canonical `node <repository>/dist/src/index.js` launch
  validation with npm-family launchers explicitly rejected at the stdio
  boundary, plus repository-driven Doctor and `start` resolution through
  `local_codex_repository` / `executable_paths.node` and Node.js 24+ version
  enforcement;
- production Runtime Composition (`supervisor.runtime.RuntimeComposition`)
  now wires the active Profile into the real Supervisor MCP surface:
  Profile B uses `LocalCodexBridgeAgentBackend` through one long-lived
  `AgentSessionManager` stdio session plus `AgentExecutionCoordinator` and
  `CheckpointService`; Profile A keeps `ControlPlaneAgentBackend` as the
  compatibility fallback. `mcp.server.create_mcp_server` accepts the neutral
  `AgentSupervisorFacade`, and `mcp.codex_tools` is provider-neutral with the
  same MCP tool names;
- Local-Codex-Bridge is now a client-owned stdio MCP session, never a detached
  daemon: `BootstrapService.start` no longer calls
  `ProcessManager.start(local_codex_bridge.process_spec())`. The Supervisor
  process owns the stdin/stdout lifecycle; restart recovery uses persisted
  `thread_id/turn_id/operation_id`, resumes only when observe confirms the
  runtime, and otherwise latches `RECONCILIATION_REQUIRED` and fails closed;
- task-level backend/profile binding (schema v8 `task_backend_binding`) fixes
  workspace/agent/profile on first runtime bind. A later capability drop
  cannot silently switch a bound task; only an explicit controlled migration
  may replace the binding;
- `AgentSessionManager` reuses one managed session across
  start_plan/observe/steer/respond/interrupt, counts session creation/shutdown,
  and performs fail-closed restart recovery. The LCB session-reuse test proves
  one active turn does not spawn five processes;
- `codex-supervisor start` uses a bounded `SUPERVISOR_READY` log marker for
  combined supervisor/agent readiness and reports DEGRADED instead of READY
  when the agent probe is not healthy; the marker itself is fail-soft and
  bounded, so an ephemeral agent probe failure cannot block Supervisor startup;
- app-managed component installation foundation
  (`bootstrap.installer.ComponentInstaller`) installs pinned manifests into
  `%LOCALAPPDATA%\CodexSupervisorBridge\components`, verifies checksums,
  runs bounded retries, promotes atomically, and rolls back to the previous
  version; `codex-supervisor repair/start` now executes these trusted
  installs, while library callers without `auto_install=True` still receive
  bounded plans for tests and GUI previews;
- Profile B Node compatibility is unified to `>=24, <27` so Doctor and the
  installer cannot report DevSpace READY while Local-Codex-Bridge later
  rejects the same runtime;
- a production-composition fake/protocol E2E now runs the full 20-step
  scenario through real MemoryService, DirectWorkspaceCoordinator,
  AgentExecutionCoordinator, and the semantic MCP facade for both profiles,
  and compares revision, plan version, writer, writer_epoch, workspace
  identity, and event sequence;
- fake process/provider harnesses for Profile A/B semantic comparison;
- ProcessManager watchdog behavior is now covered end to end: crashed-process
  detection clears the dead PID so later health checks remain CRASHED instead
  of degrading to STALE, bounded restarts fail closed at `max_restarts`, restart
  reuses the same log path, graceful stop falls back to hard kill on timeout,
  and a running component is never launched twice;
- CodexReadinessDetector is covered by the full probe matrix: missing
  executable, version-command failure with preserved diagnostic detail,
  launch exception, missing/uninitialized workspace, sign-in inference, and
  explicit absolute executable resolution;
- the `configure` CLI is covered by an end-to-end `main()` test that persists
  Basic user intent into the versioned settings file and emits normal UX JSON
  without provider names, plus an advanced JSON test for future GUI consumers.
- `BootstrapService.start` consults Doctor before launching optional
  components: incompatible DevSpace releases and non-ready Local-Codex-Bridge
  runtimes are reported as user-action repairs instead of being started. The
  healthy supervisor / workspace / Codex-control orchestration path, RepairService
  stale and UNKNOWN process handling, Doctor crash reflection, fail-closed
  SecureRemote reconnect/rotate, and Profile A/B normalized difference detection
  are covered by fake tests (the suite has grown since this earlier round).
- RepairService recovers invalid configuration from DEGRADED to safe defaults,
  and the `doctor` CLI emits structured UX JSON without provider, SQLite, or
  backend names. Doctor reports a stopped Supervisor Bridge as repairable
  `start_supervisor` instead of falsely showing it as READY. The current
  code-level suite was 171 tests at that point locally on Python 3.12/3.13,
  including task
  backend binding, AgentSessionManager reuse and fail-closed restart,
  installer atomic promote/rollback, production-composition E2E, and the
  fail-soft readiness marker.

### 2026-08-28 production safety / lifecycle convergence

The pre-real-Gate production wiring round is complete on the P6.6 branch:

- `RuntimeResolver` performs capability-driven production profile selection.
  An unbound task prefers Profile B when both workspace and agent are fully
  READY, falls back to Profile A only when Profile B is not ready, and never
  silently switches an already-bound task: `binding_forced=True`,
  `fallback_allowed=False`, and a temporary capability drop reports
  DEGRADED/UNAVAILABLE instead of switching backends;
- Profile A is now semantically consistent: `RuntimeComposition.profile_a`
  builds a `KandevWorkspaceBackend` workspace factory (not DevSpace), and
  `KandevWorkspaceBackend` selects a matching Kandev workspace and fails
  closed for direct read/patch/command operations that Kandev does not expose;
  Profile B keeps the authenticated DevSpace workspace factory;
- `RuntimeComposition.start()` starts the persistent `AgentSessionManager` and
  runs persisted-runtime recovery **before** readiness, and
  `RuntimeComposition.shutdown()` shuts the session down (safe to call more
  than once); `mcp/server.py` composes start -> recovery -> combined readiness
  marker -> MCP server -> shutdown;
- combined readiness (`ProfileReadiness`) now requires workspace probe plus
  agent/session health plus zero startup reconciliation blockers. Supervisor
  process startup is not reported as PROFILE_READY when DevSpace, LCB, or
  Codex readiness is missing; it reports DEGRADED/UNAVAILABLE instead;
- the semantic facade routes every agent call through `AgentSessionManager`
  when one is composed, so the first MCP call can be `get_codex_control_health`
  without a prior `start_codex_plan`;
- restart recovery happens before READY: a persisted CODEX writer with an
  active runtime identity is resumed through the new long-lived session.
  Confirmed resume is allowed; UNKNOWN, failed, or incomplete identity latches
  `RECONCILIATION_REQUIRED`, and the startup E2E proves handoff/mutation is
  blocked by that latch;
- `soft_steer` and `answer_interaction` now use the shared guarded remote
  mutation path: baseline revision/intent/plan/writer/epoch/runtime identity,
  post-call stale validation, and durable compensation interrupt before any
  stale result can bind. `answer_interaction` is writer-fenced by interaction
  kind: command/file/permissions approvals require the current CODEX lease,
  `user_input` is revision/runtime fenced only, `provider_request` is denied
  by default, and unknown kinds are denied;
- stale interrupt results cannot overwrite a newer runtime identity. The old
  runtime interruption is recorded as compensation evidence; if the current
  runtime cannot be confirmed the facade latches reconciliation and fails
  closed;
- backend migration is gated: an explicit migration requires no CODEX active
  writer, no unresolved safety latch, no pending direct mutation, no active
  Codex runtime, and a saved workspace snapshot; negative tests cover every
  blocker;
- `ManagedComponentRegistry` pins immutable manifests: Node.js 24.20.0 with an
  official SHA256, DevSpace 1.0.8 with a published SHA256, and
  Local-Codex-Bridge v2.1.3 with the verification strategy recorded because
  upstream publishes no official SHA256. The installer now accepts argv-only
  install commands, rejects non-built-in manifests when a trusted registry is
  configured, rejects path traversal, and RepairService exposes
  `prepare_local_environment` install plans in advanced diagnostics when
  auto-install is disabled or a trusted install fails;
- the code-level suite was 211 tests at that point locally on Python 3.12/3.13,
  including the production startup E2E (healthy Profile B, Profile A fallback,
  bound-task no-fallback, pre-READY reconciliation, first health call, clean
  shutdown, no orphan session), guarded steer/respond/interrupt race tests,
  backend migration negative tests, RuntimeResolver selection tests,
  Kandev workspace semantics, and managed component registry tests.

The current code-level work does not claim real Windows OAuth, ChatGPT Web
Remote MCP attachment, tunnel-provider login, or authenticated Codex runtime
proof. Those remain opt-in/manual gates after the fake and protocol coverage is
complete. Profile A remains the fallback until Profile B passes the documented
real Windows scenario.

### 2026-08-28 cold-start / managed installer / UNKNOWN safety round

This round closes the remaining code-level production gaps before the real
Windows Gate:

- DevSpace now starts before the Supervisor process in
  `BootstrapService.start`: managed config/auth are prepared, the local
  workspace process is launched, and only then is Supervisor started, so
  `SUPERVISOR_READY` cannot be claimed against a DevSpace that never came up;
- `ComponentInstaller` is now a real installer, not a plan-only stub: it uses
  a bounded HTTPS downloader, rejects HTTP, verifies SHA256, performs safe ZIP /
  tar extraction with no traversal/link/device escapes, runs argv-only install
  commands, promotes atomically, rolls back to the previous version, cleans
  interrupted staging, and treats a healthy same-version install as
  `ALREADY_INSTALLED` (corrupt markers are reinstalled);
- managed Node.js 24.20.0 is the Profile B runtime: `npm` install commands are
  rewritten to `managed-node.exe <npm-cli.js> ...` when the managed Node is
  installed, DevSpace is launched as `managed-node.exe <package>/dist/cli.js
  serve`, and Local-Codex-Bridge uses the managed Node plus the managed
  repository's `dist/src/index.js`; no system PATH dependency is required for
  Profile B components;
- the built-in registry pins immutable artifacts: Node.js 24.20.0 (official
  SHA256), DevSpace 1.0.8 (published SHA256), and Local-Codex-Bridge v2.1.3 at
  exact commit `4ffed814f615316ade8967189a2e1772488d33c2` (unsigned upstream
  archive; verification is the immutable GitHub commit archive plus build and
  protocol health);
- `codex-supervisor repair/start` runs with `auto_install=True`: missing
  trusted components are downloaded, extracted, verified, promoted, and their
  managed paths are persisted into Advanced settings before the doctor is
  re-run; normal UX still shows only `prepare_local_environment`;
- `RuntimeResolver` now includes the `codex` capability as a separate
  readiness input, so a healthy LCB/DevSpace pair cannot report PROFILE_READY
  when the Codex CLI is missing or unauthenticated;
- `mcp/server.py` reads persisted active `task_backend_binding` rows at
  startup: one active binding is enforced (no fallback to a healthier
  profile), and multiple active tasks bound to different profiles fail closed
  with `STARTUP_RECONCILIATION_REQUIRED`;
- `AgentSessionManager` recovery is scoped to the current composition binding:
  an active CODEX writer bound to another profile becomes a startup
  reconciliation blocker instead of being resumed by the wrong agent session;
- `CapabilityResolver` maps the public `Codex` capability to the real
  `codex` readiness probe instead of the AgentBackend health, so Kandev /
  Control Plane availability can no longer masquerade as Codex readiness;
- `soft_steer` and `answer_interaction` now fail closed when the remote
  snapshot is `UNKNOWN` / `reconciliation_required`: the shared
  `post_call_stale_reasons` includes the remote result and compensation
  interrupts before any result can bind;
- `interrupt` never writes `PAUSED` from an UNKNOWN remote outcome; it latches
  `RECONCILIATION_REQUIRED` and raises. A revision-only interrupt race (same
  runtime identity, revision changed while the remote call was in flight)
  observes the current runtime and only binds a confirmed terminal state,
  otherwise reconciliation is latched and no stale interrupt can overwrite a
  newer intent;
- new cold-start, managed-installer, binding-conflict, Codex-readiness,
  UNKNOWN-steer/respond/interrupt, revision-only race, and archive-safety tests
  are part of the PR #9 suite. The code-level suite is 234 tests locally on
  Python 3.12/3.13.

The real Windows Gate remains: install/launch DevSpace and Local-Codex-Bridge
on a user machine, complete one DevSpace authorization, log in to Codex,
create the Supervisor-only HTTPS tunnel, attach ChatGPT Web Remote MCP to the
Supervisor endpoint, and run the same Profile A/B scenario against real
worktrees. P6.6 is not marked complete until those gates pass.

### 2026-08-28 runtime affinity / redirect safety round

This round closes the remaining pre-real-Gate semantic gaps:

- runtime affinity is explicitly different from writer ownership: a task must
  keep its backend binding at startup when it has an active writer, an active
  Codex runtime (`planning` / `executing` / `running` / `inprogress` /
  `in_progress` / `started`) even with no writer, an unresolved agent safety
  latch, a PREPARED direct mutation, or a RUNNING direct command;
- `list_runtime_affinity_bindings` replaces the writer-only startup query, and
  `mcp/server.py` fails closed on multiple distinct runtime-affinity bindings
  even when one task is a read-only planning runtime and another is executing;
- `AgentSessionManager` recovers every runtime-affinity task: a read-only
  `planning` runtime is resumed with `active_writer = NONE` or `CHATGPT`
  without taking the workspace writer, while `executing` / `running` /
  `inprogress` / `in_progress` / `started` runtimes still require a current
  CODEX writer lease with `writer_epoch >= 1`, otherwise
  `RECONCILIATION_REQUIRED` is latched before READY;
- unresolved compensation/reconciliation safety latches block automatic
  recovery instead of being silently retried, and any UNKNOWN plan-mode resume
  latches `RECONCILIATION_REQUIRED` before `PROFILE_READY`;
- `HttpsDownloader` now rejects every HTTPS -> HTTP (or non-HTTPS) redirect hop
  through a `NoDowngradeRedirectHandler`; final and intermediate downgrades
  fail closed with `DownloadError`, and HTTPS -> HTTPS redirects remain allowed;
- the built-in Local-Codex-Bridge manifest now runs the explicit production
  install gate `npm ci` -> `npm run typecheck` -> `npm run build` with the
  managed Node/npm absolute paths; protocol health remains the post-install
  runtime verification;
- new plan-mode restart, execution writer-fence, conflicting affinity, and
  redirect-downgrade tests are part of the PR #9 suite.
- the code-level suite is 243 tests locally on Python 3.12/3.13.

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

Code-level production wiring for this phase is now complete in PR #9:
Profile B runs through `AgentExecutionCoordinator` + `LocalCodexBridgeAgentBackend`
via a persistent `AgentSessionManager`, Profile A remains the
`ControlPlaneAgentBackend` compatibility path, the Codex semantic MCP facade is
provider-neutral, task backend binding is durable (schema v8), and restart
recovery fails closed into reconciliation. The production safety/lifecycle
convergence round (capability-driven RuntimeResolver, pre-READY recovery,
combined readiness, guarded steer/respond/interrupt, migration gate, and
managed component registry) is also in PR #9 and green on Linux/Windows CI.
The next step is the real Windows Gate (install/launch/OAuth/Codex
login/tunnel/ChatGPT Remote MCP attachment).

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
