# Project State and Current Architecture Baseline

> **Read this first when resuming the project in a new ChatGPT conversation.**
>
> This document records the current product intent and the architecture decisions that supersede earlier fixed-backend assumptions. It is deliberately more durable than a browser conversation.

Last updated: 2026-09-04

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

#### 2026-08-31 release blocker: Codex Desktop runtime isolation

P6.6 remains **ACTIVE**. PR #9 is **NOT READY TO MERGE** and must remain open
as a Draft. Remote Profile B is **NOT production safe** until the complete
Desktop isolation Gate passes.

At least two real incidents showed Codex Desktop conversations becoming
unavailable after Supervisor/Local-Codex-Bridge activity. The visible failure
included `Codex app-server process is not available`; the later incident did
not require a repeated interrupt sequence. The earlier assumption that
`interrupt` alone caused the failure is therefore insufficient.

The first-level root cause is confirmed as
`CONFIRMED_WINDOWS_APPDATA_EXECUTION_CONTEXT_REDIRECTION`. The same requested
path under `C:\Users\Windows\AppData\Local\CodexSupervisorBridge` resolved to
different physical files from different Windows execution contexts. The
Desktop-derived child reported `PACKAGE_IDENTITY=NO_PACKAGE_IDENTITY` but still
resolved into `C:\Users\Windows\AppData\Local\Packages\OpenAI.Codex_*\LocalCache\Local`.
The canonical external context resolved to the non-packaged AppData root.
The two physical files differed in final path, file identity, size, mtime, and
SHA-256. Package identity alone cannot establish a safe Supervisor host.

#### 2026-09-03 Host ancestry TOCTOU evidence

An independent PowerShell 7.6.5 session ran the formal Host preflight. The
Physical Path Guard passed for AppData, components, Local-Codex-Bridge, and
runtime; all four physical roots resolved under the canonical
`C:\Users\Windows\AppData\Local\CodexSupervisorBridge`, and no AppData
redirection was observed in that external context. The Host verdict remained
`host_ownership=UNKNOWN` with
`failure_code=SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE` because its
`pwsh.exe -> venv python.exe -> base Python312 python.exe` ancestry could be
split across two snapshots. `GATE_0=FAILED` and `GATE_1=NOT_STARTED` remain
authoritative.

The follow-up code fix makes one Host inspection verdict snapshot-consistent:
current identity, complete ancestry classification, inherited authority, and
persisted PID checks use one snapshot-scoped process index. Parent creation-time
or executable mismatches remain `UNKNOWN` and fail closed.
`PROCESS_ANCESTRY_TOCTOU` is **FIXED** at the code/test layer. This is code and
fake-test evidence only; no real Gate was rerun.

#### 2026-09-04 trusted Windows shell launch boundary

A later Start Menu PowerShell 7.6.5 Host preflight still saw canonical physical
roots under `C:\Users\Windows\AppData\Local\CodexSupervisorBridge`. The
observed chain was Host -> venv Python -> PowerShell -> canonical
`Explorer.exe`. Explorer's historical parent was absent from the current
snapshot, so the verdict remained `host_ownership=UNKNOWN` /
`SUPERVISOR_HOST_EXECUTION_CONTEXT_UNSAFE`. `GATE_0=FAILED` and
`GATE_1=NOT_STARTED`. Do not treat Gate 0 as passed.

The trusted Explorer launch boundary is implemented, but a later real Gate 0
preflight still returned `host_ownership=UNKNOWN` with
`host_launch_boundary_verified=false` while `physical_paths_verified=true`.
The remaining code-level root cause is
`WINDOWS_EXPLORER_BOUNDARY_PARENT_METADATA_CONTRADICTION`: production
`ProcessInspector._windows_snapshot()` leaves Explorer
`parent_creation_time`/`parent_executable` as `None` when the historical
parent is gone, and the first completeness check incorrectly required those
fields. Explorer self-identity is now separate from parent metadata. If the
Explorer parent is still in the snapshot, mismatch stays `UNKNOWN`.
`ProcessInspector` is unchanged. Fake explorer paths, Desktop ancestry, PID
reuse, and missing non-Explorer parents stay fail-closed. Linux `/proc`
behavior is unchanged. This is code and fake-test evidence only; no real
Gate 0 was rerun. `GATE_1=NOT_STARTED`.

Read-only investigation of the pinned Local-Codex-Bridge 2.1.3 source at
commit `4ffed814f615316ade8967189a2e1772488d33c2` confirmed that LCB starts its
own `codex app-server --listen stdio://` child and uses protocol-level
`turn/interrupt`; it does not intentionally attach to a Codex Desktop
app-server. Two independent unsafe gaps were confirmed:

- the LCB child inherited the user's default Codex environment and `CODEX_HOME`,
  so Desktop and LCB could share the persistent conversation/session state root;
- LCB's Windows hard-stop path used `taskkill.exe /PID <pid> /T /F` after only
  checking the Node `ChildProcess` PID/exit state. It had no creation time,
  command fingerprint, parent identity, runtime instance, or ownership token
  validation before terminating the process tree.

The same lifecycle gap remained on the checked upstream `main` revision
`ff5d880`, so a plain LCB upgrade did not resolve it. This combination meant a
Supervisor/LCB failure could not be shown independent from the user's daily
Desktop runtime.

The current branch freezes a standalone-host boundary. PHASE A0/A1 adds
`StandaloneSupervisorHost` as the Supervisor lifecycle authority and
`PhysicalPathGuard` as a fail-closed handle-based physical namespace check for
AppData, components, runtime, LCB, CODEX_HOME, writes, process spawn, repair,
installation, and tunnel startup. PHASE A1 also makes
`CodexExecutableResolver` the runtime preparation path, classifies stale
legacy identities through `ProcessManager`, and binds hardening validation to
the actual LCB launch entrypoint. Supervisor service children inherit the
verified Host authority explicitly and cannot overwrite the parent-owned Host
identity record. An unresolved or unsafe executable, root, or ownership identity
cannot fall back to the Desktop app-server.

The installed Codex inspected on 2026-08-30 is
`codex-cli 0.151.0-alpha.7.2`. Its current help exposes direct app-server stdio,
daemon, and proxy modes. A passive process snapshot confirmed the active
Desktop app-server was a `codex.exe` child of `ChatGPT.exe`; no active
Supervisor/LCB/tunnel runtime was detected. No process was terminated.

The code-level blocker work on this branch now establishes:

- `DESKTOP_EXTERNAL`, `SUPERVISOR_MANAGED`, and `UNKNOWN` process ownership;
- PID-reuse-resistant identity using creation time, executable, command-line
  fingerprint, parent PID, and parent process identity;
- a canonical Supervisor runtime namespace under
  `runtime/codex/<instance-id>` with an isolated `CODEX_HOME`, LCB checkpoint
  directory, ownership-token hash, process-chain metadata, and private stdio
  endpoint category;
- a Supervisor-owned runtime proxy that verifies the
  Supervisor -> proxy -> LCB -> Codex app-server parent/child chain before
  Profile B can report READY;
- a trusted managed LCB source hardening contract (`supervisor-runtime-v1`,
  revision `csb-lcb-runtime-1`) applied before the pinned source is built;
  the marker binds both the patched TypeScript source and the built
  `dist/src/app-server.js` / `dist/src/supervisor-runtime.js` digests;
  Doctor, installer repair, and Profile B startup reject an absent, stale, or
  digest-mismatched source or build marker as
  `LCB_RUNTIME_ISOLATION_UNSUPPORTED`;
- LCB capture and revalidation of the app-server PID, creation time, executable,
  command-line fingerprint, parent PID, and parent identity before stdin close,
  soft termination, or Windows hard tree termination. PID reuse and incomplete
  identity fail closed;
- the hardened LCB verifies that the Codex app-server `initialize` response
  reports the Supervisor namespace's `CODEX_HOME`; Linux identity capture uses
  `/proc` start time, executable, command-line fingerprint, and parent identity
  for the same fail-closed checks as Windows;
- an ownership token that reaches the hardened LCB only. Supervisor contract,
  token, metadata, instance, and epoch variables are removed before LCB spawns
  the Codex child, and no secret value is persisted or logged;
- runtime-instance and epoch affinity for task/thread/turn/pending interaction
  recovery, with old affinity invalidated after runtime replacement;
- protocol-only turn interrupt, with no process-kill fallback;
- stalled-turn detection based on elapsed time, status transitions, pending
  interactions, latest plan, and semantic progress rather than a fixed raw
  event count;
- a runtime circuit breaker that blocks repeated start/interrupt loops until
  explicit verified recovery;
- capability fallback to Control Plane when isolated LCB runtime support is
  unavailable, without any fallback attach to Codex Desktop;
- passive Desktop process metadata in advanced diagnostics only; normal
  Context Packs omit PIDs and all credential values.

The hardened source patch also passes TypeScript typecheck and build against the
pinned LCB commit. Its patched upstream suite passes 67 tests with one platform
skip, and the platform smoke reports `TRAY_CORE_TESTS_OK`. This is still
code/fake-test evidence only. It does **not** complete the
release blocker. No second real Codex app-server, real process termination, Desktop
concurrency test, real turn interrupt Gate, or runtime crash Gate may run
without the explicit human checkpoints documented for P6.6. Until those Gates
prove bidirectional failure isolation, do not resume the full remote Profile B
E2E sequence and do not enter P7.
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
  when the Codex CLI cannot successfully execute a bounded runtime probe;
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

The Windows local-machine Gate has now been executed in the real development
environment. It passed Bridge editable install, managed Node.js 24.20.0,
managed DevSpace 1.0.8, managed Local-Codex-Bridge 2.1.3, DevSpace OAuth with
Windows DPAPI, the configured third-party Codex provider, a real read-only
Codex runtime smoke (`SUCCESS` / `READY` at least once), Supervisor startup,
persistent Local-Codex-Bridge session reuse, the Profile B local workflow,
direct-to-Codex handoff, plan approval, execution, active-turn steer,
checkpoint, interrupt, handback, and restart/resume. A later intermittent
`PROVIDER_TIMEOUT` from the third-party provider is recorded as external
runtime/service availability, not as a Bridge login failure; the readiness
probe must remain `DEGRADED` when that smoke times out.

That real run exposed one final Windows path-consistency issue. Ordinary
PowerShell and a packaged Codex child can report different `LOCALAPPDATA`
values. `bootstrap.paths` now resolves the canonical root through the Windows
Known Folder API, falls back to `USERPROFILE\\AppData\\Local`, recognizes generic
`AppData\\Local\\Packages\\<package>\\LocalCache\\Local` virtualization, and
passes the canonical root explicitly to Bridge-owned children. Physical aliases
are reported as aliases rather than split brain. An independent root containing
persistent `settings`/SecretStore state is classified as
`SPLIT_BRAIN_DETECTED`; Doctor/Repair/Start/Status fail closed and never merge,
overwrite, or delete either root. Only an unambiguous legacy-only state can use
the backup, copy, validate, atomic-promote migration path, after which the
legacy root is retained and marked inactive.

The current machine inspection initially found the canonical root at
`%USERPROFILE%\\AppData\\Local\\CodexSupervisorBridge`, a Codex packaged path
that is the same physical root (alias), and a separate Python packaged root
with its own `settings.json` and DPAPI SecretStore but no database. The
explicit two-phase reconciliation gate was then executed with **canonical** as
the user-selected authority. The Python packaged root was copied to a
timestamped backup, validated, and marked inactive; it was not deleted and no
settings, database, or secrets were merged. The root report is now `CLEAN`,
and the inactive root remains discoverable for diagnostics. The AppData portion
of the Windows local Gate is therefore passed. Overall Doctor/Repair/Start/Status
can still report `DEGRADED` when the machine contains an unrelated unknown live
PID; that condition is kept fail-closed and was not force-killed.

P6.6 remains active and is not complete. The next real Gate is only:
Supervisor HTTPS -> ChatGPT Web Remote MCP -> ChatGPT/Supervisor Profile B
end-to-end flow plus the corresponding remote Profile A/B comparison. The
resulting local suite is 295 tests on Windows Python 3.12/3.13; Ruff,
compileall, and `git diff --check` are green.

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

### 2026-08-28 provider-neutral Codex authentication / readiness round

This round freezes the product semantics for Codex readiness:

- `Codex READY` means the user's currently configured Codex provider can
  successfully execute a bounded read-only runtime probe. It does **not** mean
  ChatGPT account login, an OpenAI API key, or any specific provider name;
- `CodexAuthMode` is provider-neutral and supports `CHATGPT_ACCOUNT`,
  `OPENAI_API_KEY`, `PROVIDER_ENV_KEY`, `CUSTOM_PROVIDER`, `LOCAL_NO_AUTH`,
  `AWS_OR_CLOUD_PROVIDER`, and `UNKNOWN` for advanced diagnostics only;
- `CodexConfigInspector` reads `$CODEX_HOME/config.toml` (default
  `~/.codex/config.toml`) read-only with a size limit and fail-closed parse
  handling. It never opens `auth.json`, never modifies config, and never
  resolves an `env_key` to its value;
- a third-party provider configured with `model_provider`, `base_url`, and
  `env_key` is supported without Bridge taking ownership of the key. The
  inspector records only the variable name and whether it exists;
- `CodexReadinessDetector` is layered: executable -> version -> config
  inspection -> bounded runtime smoke. Final READY requires
  `codex exec ... --sandbox read-only --json --ephemeral
  --output-last-message <tmp> --cd <workspace>` to return
  `CODEX_RUNTIME_READY`;
- smoke failures are classified without dumping provider responses:
  missing CLI (`UNAVAILABLE`), invalid config (`DEGRADED`), missing env
  reference (`DEGRADED` + user action), provider 401/403 (`DEGRADED` + user
  action), network timeout (`DEGRADED`), model unavailable (`DEGRADED` + user
  action), and unexpected output (`DEGRADED`);
- normal UX shows only `Codex: READY` / `Codex 当前配置无法使用。` /
  `Codex 当前凭据不可用。`; provider names, env variable names, and masked
  base URLs appear only in advanced diagnostics, and credential values never
  appear anywhere;
- `lcb_environment()` inherits the user environment so LCB -> Codex sees the
  provider credentials the user already has, but the values are never
  serialized into diagnostics, Context Packs, logs, or MCP responses;
- `ProfileReadiness` combines workspace, agent, and `codex` readiness, so an
  LCB protocol that is healthy cannot make `PROFILE_READY` true when the
  Codex runtime probe is not ready;
- the Windows real-machine flow no longer defaults to `codex login`. It
  inspects the existing Codex config, detects the current provider, runs the
  runtime smoke, and only asks for credential setup when the smoke explicitly
  proves the current credential is unusable;
- the new provider-neutral readiness tests cover ChatGPT account, OpenAI API
  key, third-party `env_key`, local no-auth, missing env reference, provider
  401, timeout, model unavailable, invalid config, secret redaction, no
  forced login/logout, and ProfileReadiness combining the real Codex runtime
  result. The code-level suite is 307 tests locally on Python 3.12/3.13.

### 2026-08-29 explicit Windows AppData split-brain reconciliation

The final P6.6 local blocker was exercised on the real Windows machine with
the new two-phase `codex-supervisor reconcile-app-data` command. The logical
canonical root and its physical storage root are both
`C:\\Users\\Windows\\AppData\\Local\\CodexSupervisorBridge`; the Codex
packaged path is a physical alias, not a second state root. The independent
Python 3.13 packaged root was selected explicitly as the non-authoritative
root:
`C:\\Users\\Windows\\AppData\\Local\\Packages\\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\\LocalCache\\Local\\CodexSupervisorBridge`.

The dry-run plan selected `canonical`, bound the plan id to both root
fingerprints, and reported:

- canonical state: `database`, `settings`, `secrets`, `runtime`,
  `components`, `logs`, `cache`;
- legacy state: `settings`, `secrets`, `components`, `cache`;
- persistent state: canonical `database`, `settings`, `secrets`, `runtime`,
  legacy `settings`, `secrets`;
- settings relation: `DIFFERENT`, using `AppConfig`/`ConfigStore` semantic
  parsing rather than text comparison;
- secret relation: `DIFFERENT`; canonical had 3 names, legacy had 1, with
  one same name and two canonical-only names. Secret values were never read,
  compared, logged, or serialized;
- database relation: `CANONICAL_ONLY`; canonical schema 8 had one task and no
  unresolved/prepared/running mutation, while the legacy database was absent;
- runtime relation: `CANONICAL_ONLY`; the legacy root had no live runtime.

The confirmed apply copied and validated the complete legacy root before
writing the inactive marker. Backup evidence is at
`C:\\Users\\Windows\\AppData\\Local\\CodexSupervisorBridge\\.reconciliation-backups\\20260829T095459795376Z-a2f605bdf329\\legacy`.
The marker is
`C:\\Users\\Windows\\AppData\\Local\\Packages\\PythonSoftwareFoundation.Python.3.13_qbz5n2kfra8p0\\LocalCache\\Local\\CodexSupervisorBridge\\.codex-supervisor-legacy-inactive.json`.
The original root remains discoverable and untouched; a second reconciliation
is idempotent and reports `ALREADY_RECONCILED`. The post-apply root report is
`CLEAN`, and both ordinary PowerShell and the packaged Python 3.12/3.13
startup paths resolve `AppDataPaths.root` to the canonical root without
reactivating the inactive Python root.

The local Profile B regression (read -> plan -> approve -> handoff -> execute
-> checkpoint -> handback -> restart -> resume/status) completed against the
same canonical database, settings, SecretStore, backend binding, and writer
epoch. Codex readiness reached `SUCCESS`/`READY` during the real smoke; a later
third-party provider timeout and an unrelated unknown `cmd /c claude-code-mcp`
PID are recorded as external runtime availability, so overall diagnostics may
be `DEGRADED` while Application data remains `READY`. No `taskkill` or
destructive root cleanup was used.

The Windows local AppData Gate is **PASSED**. The next phase is the separately
scoped Supervisor HTTPS / ChatGPT Web Remote MCP Gate; P7 remains unopened.

### 2026-08-29 Secure MCP Tunnel local integration

P6.6 now freezes the remote transport as the OpenAI-hosted Secure MCP Tunnel
architecture. The intended path is:

```text
ChatGPT Web Custom MCP App
  -> OpenAI Secure MCP Tunnel endpoint
  -> outbound HTTPS tunnel-client
  -> http://127.0.0.1:<supervisor-port>/mcp
  -> Supervisor Bridge -> DevSpace / Local-Codex-Bridge -> Codex
```

DevSpace, Local-Codex-Bridge, and Supervisor remain loopback-only. The Bridge
does not open a public listener, configure Cloudflare/ngrok, or expose raw
shell/filesystem access. The old generic HTTPS abstraction remains available
as a vendor-neutral fallback, while the Windows default is
`OPENAI_SECURE_MCP_TUNNEL`.

The official `openai/tunnel-client` v0.0.13 Windows amd64 release is now a
trusted managed component. The registry pins the GitHub release URL, artifact
SHA256, and official `SHA256SUMS.txt` sidecar; installation uses the existing
safe archive extractor, atomic promotion/rollback, and binary `--version`
verification. Windows arm64 selection is represented by the same pinned
manifest with its official arm64 checksum.

Remote settings persist only provider-neutral metadata (`tunnel_id`, loopback
MCP URL, loopback health listener, client version, and
`runtime_secret_ref`). The runtime API key is stored only in the Windows DPAPI
SecretStore and is injected into the managed child through an environment
variable reference. It is never written to settings, argv, process metadata,
logs, diagnostics, Context Pack, MCP responses, or project documentation.

`codex-supervisor remote configure --tunnel-id <id>` performs hidden terminal
input and stores the runtime key locally. It does not create a tunnel and does
not accept an admin key. Doctor/Status report process, `/healthz`, `/readyz`,
local MCP target, client version, and key presence without exposing the key.
Remote access is READY only when the managed process is running and both
health endpoints are ready; this capability is kept separate from Codex
inference readiness.

The historical Supervisor PID `28268` was inspected with executable,
command-line, creation time, parent PID, and listener evidence. It is a live
`cmd.exe /d /s /c claude-code-mcp` PID reuse, not a Bridge process. The Bridge
runtime record was cleared through ProcessManager's identity check without
terminating the unrelated process. New managed records persist executable,
start time, command fingerprint, and managed instance identity; mismatches
are classified as `PID_REUSED`/`STALE_IDENTITY` and fail closed.

The local Secure MCP Tunnel integration is code-complete and the managed
binary is installed. OpenAI Platform tunnel/runtime-key creation and ChatGPT
Workspace Developer Mode / Custom MCP App actions remain explicit human gates;
P6.6 remains ACTIVE until the remote Profile B live scenario completes.

### 2026-08-29 first live ChatGPT Secure MCP Tunnel verification

The first live remote access check has now completed on the Windows machine.
From a new ChatGPT conversation (`Codex插件控制测试`), the Custom MCP App was
able to reach the local Codex runtime through the configured OpenAI Secure MCP
Tunnel. This confirms the intended transport path is live:

```text
ChatGPT Web -> Custom MCP App -> OpenAI Secure MCP Tunnel
  -> tunnel-client 0.0.13 -> http://127.0.0.1:8767/mcp
  -> Supervisor Bridge -> Local-Codex-Bridge -> Codex
```

At verification time the local evidence was:

- tunnel id: `tunnel_6a92f2b222788191b5619aebcce4cd5f`;
- tunnel-client process identity: managed `0.0.13` child, PID `30572`;
- local Supervisor MCP: `http://127.0.0.1:8767/mcp`;
- loopback health listener: `http://127.0.0.1:52411`;
- `/healthz`: HTTP 200;
- `/readyz`: HTTP 200;
- runtime key: present in Windows DPAPI SecretStore only; no value was
  displayed, serialized, or sent through the chat;
- Supervisor, DevSpace, and Local-Codex-Bridge remained loopback-only.

The tunnel-client log also records an `openai-mcp-discover` request followed by
forwarded MCP commands, and the Supervisor log records the corresponding
loopback `/mcp` requests. The canonical Profile B task memory remains durable
through this access check: task `P66-GATE-E5062DF-A` is at revision 28 with
`writer_epoch=3`, and its persisted event history includes plan creation and
approval, Codex handoff, active-turn steer, checkpoint creation, interrupt, and
handback. These are evidence of transport and memory continuity; they are not
treated as proof that every remote acceptance step has passed.

The full remote Profile B acceptance sequence is intentionally still open.
Direct semantic write, Plan Gate approval, Codex execution, soft steer,
interrupt, hard replan, handback, fresh-conversation Context Pack resume,
tunnel-only restart, Supervisor restart, and network-recovery evidence have not
yet been recorded as a complete end-to-end set. Therefore P6.6 remains ACTIVE
and PR #9 remains Draft; do not enter P7 or merge the PR based on this first
access check alone.

The local verification rerun on 2026-08-29 passed the complete test suite,
Ruff, `python -m compileall src`, and `git diff --check`. Pytest emitted only
Windows ACL warnings while writing `.pytest_cache` and its temporary cleanup
directory; no test failed.

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
The Windows local install/runtime portion has passed; the next step is the
Supervisor HTTPS -> ChatGPT Web Remote MCP attachment and the complete remote
Profile A/B scenario. Do not enter P7 until that Gate is explicitly accepted.

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
