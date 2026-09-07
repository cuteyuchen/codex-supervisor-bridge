from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .physical import PhysicalPathGuard, PhysicalPathVerificationError

LCB_RUNTIME_CONTRACT = "supervisor-runtime-v1"
LCB_HARDENING_REVISION = "csb-lcb-runtime-1"
LCB_RUNTIME_MARKER = ".codex-supervisor-runtime-contract.json"
LCB_RUNTIME_BUILD_FILES = (
    "dist/src/app-server.js",
    "dist/src/supervisor-runtime.js",
)


class LcbHardeningError(RuntimeError):
    """The Local-Codex-Bridge source cannot be made ownership-safe."""


_SUPERVISOR_RUNTIME_TS = r'''import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { readFileSync, readlinkSync } from "node:fs";
import path from "node:path";

export const SUPERVISOR_RUNTIME_CONTRACT = "supervisor-runtime-v1";
export const SUPERVISOR_HARDENING_REVISION = "csb-lcb-runtime-1";

const CONTRACT_ENV = "CODEX_SUPERVISOR_RUNTIME_CONTRACT";
const INSTANCE_ENV = "CODEX_SUPERVISOR_RUNTIME_INSTANCE_ID";
const EPOCH_ENV = "CODEX_SUPERVISOR_RUNTIME_EPOCH";
const TOKEN_ENV = "CODEX_SUPERVISOR_OWNERSHIP_TOKEN";
const METADATA_ENV = "CODEX_SUPERVISOR_RUNTIME_METADATA";

export interface ProcessIdentity {
  readonly pid: number;
  readonly creationTime: string;
  readonly executable: string;
  readonly commandFingerprint: string;
  readonly parentPid: number;
  readonly parentCreationTime: string;
  readonly parentExecutable: string;
}

export interface SupervisorRuntimeBinding {
  readonly instanceId: string;
  readonly runtimeEpoch: number;
  readonly metadataPath: string;
  readonly ownershipTokenHash: string;
  readonly codexHome: string;
}

const WINDOWS_IDENTITY_SCRIPT = String.raw`
$targetPid = __PID__
$p = Get-CimInstance Win32_Process -Filter "ProcessId=$targetPid" -ErrorAction SilentlyContinue
if ($null -eq $p) { exit 2 }
$parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($p.ParentProcessId)" -ErrorAction SilentlyContinue
$commandHash = ''
if ($p.CommandLine) {
  $sha256 = [System.Security.Cryptography.SHA256]::Create()
  try {
    $bytes = [System.Text.Encoding]::UTF8.GetBytes([string]$p.CommandLine)
    $commandHash = -join ($sha256.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
  } finally {
    $sha256.Dispose()
  }
}
[pscustomobject]@{
  pid = [int]$p.ProcessId
  creationTime = if ($p.CreationDate) { $p.CreationDate.ToUniversalTime().ToString('o') } else { '' }
  executable = if ($p.ExecutablePath) { [string]$p.ExecutablePath } else { [string]$p.Name }
  commandFingerprint = $commandHash
  parentPid = [int]$p.ParentProcessId
  parentCreationTime = if ($parent -and $parent.CreationDate) { $parent.CreationDate.ToUniversalTime().ToString('o') } else { '' }
  parentExecutable = if ($parent -and $parent.ExecutablePath) { [string]$parent.ExecutablePath } elseif ($parent) { [string]$parent.Name } else { '' }
} | ConvertTo-Json -Compress
`;

function readJson(pathname: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(readFileSync(pathname, "utf8"));
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("Supervisor runtime metadata is not an object");
  }
  return parsed as Record<string, unknown>;
}

function nonEmptyString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`Supervisor runtime ${name} is missing`);
  }
  return value;
}

function metadataBinding(pathname: string, environment: NodeJS.ProcessEnv): SupervisorRuntimeBinding {
  const metadata = readJson(pathname);
  const instanceId = nonEmptyString(environment[INSTANCE_ENV], "instance id");
  const token = nonEmptyString(environment[TOKEN_ENV], "ownership token");
  const epochText = nonEmptyString(environment[EPOCH_ENV], "runtime epoch");
  const runtimeEpoch = Number(epochText);
  if (!Number.isInteger(runtimeEpoch) || runtimeEpoch < 1) {
    throw new Error("Supervisor runtime epoch is invalid");
  }
  if (metadata.instance_id !== instanceId || metadata.runtime_epoch !== runtimeEpoch) {
    throw new Error("Supervisor runtime metadata identity mismatch");
  }
  if (
    metadata.lcb_runtime_contract !== SUPERVISOR_RUNTIME_CONTRACT ||
    metadata.lcb_hardening_revision !== SUPERVISOR_HARDENING_REVISION
  ) {
    throw new Error("Supervisor runtime lifecycle contract mismatch");
  }
  if (metadata.ownership !== "SUPERVISOR_MANAGED") {
    throw new Error("Supervisor runtime ownership is not SUPERVISOR_MANAGED");
  }
  const codexHome = nonEmptyString(metadata.codex_home, "Codex home");
  const ownershipTokenHash = createHash("sha256").update(token, "utf8").digest("hex");
  if (metadata.ownership_token_hash !== ownershipTokenHash) {
    throw new Error("Supervisor runtime ownership token mismatch");
  }
  return {
    instanceId,
    runtimeEpoch,
    metadataPath: path.resolve(pathname),
    ownershipTokenHash,
    codexHome,
  };
}

function posixProcessIdentity(pid: number): ProcessIdentity | null {
  if (process.platform !== "linux") {
    return null;
  }
  try {
    const stat = readFileSync(`/proc/${pid}/stat`, "utf8");
    const closingParen = stat.lastIndexOf(")");
    if (closingParen < 0) {
      return null;
    }
    const fields = stat.slice(closingParen + 2).trim().split(/\s+/);
    const parentPid = Number(fields[1]);
    const creationTime = fields[19];
    if (!Number.isInteger(parentPid) || parentPid <= 0 || !creationTime) {
      return null;
    }
    const command = readFileSync(`/proc/${pid}/cmdline`);
    const executable = readlinkSync(`/proc/${pid}/exe`);
    const parentStat = readFileSync(`/proc/${parentPid}/stat`, "utf8");
    const parentClosingParen = parentStat.lastIndexOf(")");
    if (parentClosingParen < 0) {
      return null;
    }
    const parentFields = parentStat.slice(parentClosingParen + 2).trim().split(/\s+/);
    const parentCreationTime = parentFields[19];
    const parentExecutable = readlinkSync(`/proc/${parentPid}/exe`);
    if (!parentCreationTime || !parentExecutable) {
      return null;
    }
    return {
      pid,
      creationTime,
      executable,
      commandFingerprint: createHash("sha256").update(command).digest("hex"),
      parentPid,
      parentCreationTime,
      parentExecutable,
    };
  } catch {
    return null;
  }
}

export function readSupervisorRuntimeBinding(
  environment: NodeJS.ProcessEnv = process.env,
): SupervisorRuntimeBinding | undefined {
  const contract = environment[CONTRACT_ENV]?.trim();
  if (!contract) {
    return undefined;
  }
  if (contract !== SUPERVISOR_RUNTIME_CONTRACT) {
    throw new Error("Unsupported Supervisor runtime contract");
  }
  const metadataPath = nonEmptyString(environment[METADATA_ENV], "metadata path");
  return metadataBinding(metadataPath, environment);
}

function processIdentityFromJson(value: unknown): ProcessIdentity | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const row = value as Record<string, unknown>;
  if (
    typeof row.pid !== "number" ||
    typeof row.creationTime !== "string" || row.creationTime.length === 0 ||
    typeof row.executable !== "string" || row.executable.length === 0 ||
    typeof row.commandFingerprint !== "string" || row.commandFingerprint.length === 0 ||
    typeof row.parentPid !== "number" || row.parentPid <= 0 ||
    typeof row.parentCreationTime !== "string" || row.parentCreationTime.length === 0 ||
    typeof row.parentExecutable !== "string" || row.parentExecutable.length === 0
  ) {
    return null;
  }
  return {
    pid: row.pid,
    creationTime: row.creationTime,
    executable: row.executable,
    commandFingerprint: row.commandFingerprint,
    parentPid: row.parentPid,
    parentCreationTime: row.parentCreationTime,
    parentExecutable: row.parentExecutable,
  };
}

export function captureProcessIdentity(
  pid: number | undefined,
  attempts = 3,
  retryDelayMs = 20,
): ProcessIdentity | null {
  if (pid === undefined || !Number.isInteger(pid) || pid <= 0) {
    return null;
  }
  if (process.platform !== "win32") {
    return posixProcessIdentity(pid);
  }
  const script = WINDOWS_IDENTITY_SCRIPT.replace("__PID__", String(pid));
  for (let attempt = 0; attempt < Math.max(1, attempts); attempt += 1) {
    const result = spawnSync(
      "powershell.exe",
      ["-NoProfile", "-NonInteractive", "-Command", script],
      { shell: false, windowsHide: true, encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] },
    );
    if (result.status === 0 && !result.error) {
      try {
        const identity = processIdentityFromJson(JSON.parse(String(result.stdout ?? "")));
        if (identity !== null) {
          return identity;
        }
      } catch {
        // Retry boundedly while Windows publishes the complete process identity.
      }
    }
    if (attempt + 1 < attempts && retryDelayMs > 0) {
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, retryDelayMs);
    }
  }
  return null;
}

function sameIdentity(expected: ProcessIdentity, current: ProcessIdentity): boolean {
  return expected.pid === current.pid &&
    expected.creationTime === current.creationTime &&
    path.normalize(expected.executable).toLowerCase() === path.normalize(current.executable).toLowerCase() &&
    expected.commandFingerprint === current.commandFingerprint &&
    expected.parentPid === current.parentPid &&
    expected.parentCreationTime === current.parentCreationTime &&
    path.normalize(expected.parentExecutable ?? "").toLowerCase() === path.normalize(current.parentExecutable ?? "").toLowerCase();
}

function samePath(expected: string, current: string): boolean {
  const left = path.normalize(path.resolve(expected));
  const right = path.normalize(path.resolve(current));
  return process.platform === "win32"
    ? left.toLowerCase() === right.toLowerCase()
    : left === right;
}

function initializedCodexHome(value: unknown): string | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return null;
  }
  const codexHome = (value as Record<string, unknown>).codexHome;
  return typeof codexHome === "string" && codexHome.length > 0 ? codexHome : null;
}

export function assertInitializedCodexHome(
  binding: SupervisorRuntimeBinding,
  initializeResult: unknown,
): void {
  const initializedHome = initializedCodexHome(initializeResult);
  if (initializedHome === null || !samePath(binding.codexHome, initializedHome)) {
    throw new Error("Codex app-server initialized with an unexpected CODEX_HOME");
  }
}

function assertBindingStillValid(binding: SupervisorRuntimeBinding): void {
  const metadata = readJson(binding.metadataPath);
  if (
    metadata.instance_id !== binding.instanceId ||
    metadata.runtime_epoch !== binding.runtimeEpoch ||
    metadata.lcb_runtime_contract !== SUPERVISOR_RUNTIME_CONTRACT ||
    metadata.lcb_hardening_revision !== SUPERVISOR_HARDENING_REVISION ||
    metadata.ownership !== "SUPERVISOR_MANAGED" ||
    metadata.ownership_token_hash !== binding.ownershipTokenHash
  ) {
    throw new Error("Supervisor runtime ownership is no longer verified");
  }
}

export function assertOwnedProcess(
  binding: SupervisorRuntimeBinding,
  expected: ProcessIdentity | null,
): void {
  assertBindingStillValid(binding);
  if (expected === null) {
    throw new Error("Supervisor process identity is unavailable; termination refused");
  }
  const current = captureProcessIdentity(expected.pid, 3, 20);
  if (current === null || !sameIdentity(expected, current)) {
    throw new Error("Supervisor process identity changed; termination refused");
  }
}

export type { ProcessIdentity as SupervisorProcessIdentity };
'''


def apply_lcb_runtime_hardening(
    source_root: str | Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> Path:
    """Apply the deterministic Supervisor lifecycle patch to an LCB source tree."""

    root = Path(source_root)
    guard = path_guard or PhysicalPathGuard()
    guard.verify_root(root, role="lcb", require_directory=True)
    app_server = root / "src" / "app-server.ts"
    runtime_path = root / "src" / "supervisor-runtime.ts"
    marker = root / LCB_RUNTIME_MARKER
    for path in (app_server, runtime_path, marker):
        guard.verify_subpath(path, root, role="lcb")
    if not app_server.is_file():
        raise LcbHardeningError("LCB_RUNTIME_ISOLATION_UNSUPPORTED: src/app-server.ts is missing")
    current = app_server.read_text(encoding="utf-8")
    if _marker_is_valid(root, path_guard=guard):
        return marker
    if "readSupervisorRuntimeBinding" in current:
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: incomplete Supervisor lifecycle patch"
        )

    imports = (
        'import { platformPolicyFor, type PlatformPolicy } from "./platform.js";\n'
        'import {\n'
        '  assertInitializedCodexHome,\n'
        '  assertOwnedProcess,\n'
        '  captureProcessIdentity,\n'
        '  readSupervisorRuntimeBinding,\n'
        '  type ProcessIdentity,\n'
        '  type SupervisorRuntimeBinding,\n'
        '} from "./supervisor-runtime.js";\n'
    )
    current = _replace_once(
        current,
        'import { platformPolicyFor, type PlatformPolicy } from "./platform.js";\n',
        imports,
    )
    current = _replace_once(
        current,
        "export async function terminateAppServerChild(\n  child: ChildProcessWithoutNullStreams,\n  platformPolicy: PlatformPolicy,\n  timeouts: ChildTerminationTimeouts = DEFAULT_CHILD_TERMINATION_TIMEOUTS,\n): Promise<void> {\n  if (platformPolicy.hasChildExited(child)) {\n    return;\n  }\n",
        "export interface ChildOwnershipGuard {\n  readonly runtimeBinding: SupervisorRuntimeBinding;\n  readonly expectedIdentity: ProcessIdentity | null;\n}\n\nexport async function terminateAppServerChild(\n  child: ChildProcessWithoutNullStreams,\n  platformPolicy: PlatformPolicy,\n  timeouts: ChildTerminationTimeouts = DEFAULT_CHILD_TERMINATION_TIMEOUTS,\n  ownershipGuard?: ChildOwnershipGuard,\n): Promise<void> {\n  if (platformPolicy.hasChildExited(child)) {\n    return;\n  }\n  const verifyOwnership = (): void => {\n    if (ownershipGuard) {\n      assertOwnedProcess(ownershipGuard.runtimeBinding, ownershipGuard.expectedIdentity);\n    }\n  };\n  verifyOwnership();\n",
    )
    current = _replace_once(
        current,
        "  let terminationError: Error | undefined;\n  try {\n    child.stdin.end();\n",
        "  let terminationError: Error | undefined;\n  try {\n    verifyOwnership();\n    child.stdin.end();\n",
    )
    current = _replace_once(
        current,
        "  try {\n    platformPolicy.softTerminateChild(child);\n",
        "  try {\n    verifyOwnership();\n    platformPolicy.softTerminateChild(child);\n",
    )
    current = _replace_once(
        current,
        "  try {\n    platformPolicy.hardTerminateChild(child);\n",
        "  try {\n    verifyOwnership();\n    platformPolicy.hardTerminateChild(child);\n",
    )
    current = _replace_once(
        current,
        "  delete childEnvironment.CONTROL_PLANE_API_KEY;\n  return childEnvironment;\n",
        "  delete childEnvironment.CONTROL_PLANE_API_KEY;\n  delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_CONTRACT;\n  delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_INSTANCE_ID;\n  delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_EPOCH;\n  delete childEnvironment.CODEX_SUPERVISOR_OWNERSHIP_TOKEN;\n  delete childEnvironment.CODEX_SUPERVISOR_RUNTIME_METADATA;\n  delete childEnvironment.CODEX_SUPERVISOR_PARENT_PID;\n  return childEnvironment;\n",
    )
    current = _replace_once(
        current,
        "  #stdoutBuffer = Buffer.alloc(0);\n",
        "  #stdoutBuffer = Buffer.alloc(0);\n  #supervisorRuntimeBinding: SupervisorRuntimeBinding | undefined;\n  #childIdentity: ProcessIdentity | null = null;\n",
    )
    current = _replace_once(
        current,
        "    this.#platformPolicy = options.platformPolicy ?? platformPolicyFor();\n    this.#executable = options.executable ?? resolveCodexExecutable(sourceEnvironment);\n",
        "    this.#platformPolicy = options.platformPolicy ?? platformPolicyFor();\n    this.#supervisorRuntimeBinding = readSupervisorRuntimeBinding(sourceEnvironment);\n    this.#executable = options.executable ?? resolveCodexExecutable(sourceEnvironment);\n",
    )
    current = _replace_once(
        current,
        "      child.once(\"spawn\", onSpawn);\n        child.once(\"error\", onError);\n      });\n      child.on(\"error\", (error) => this.#onChildError(child, error));\n",
        "      child.once(\"spawn\", onSpawn);\n        child.once(\"error\", onError);\n      });\n      if (this.#supervisorRuntimeBinding) {\n        this.#childIdentity = captureProcessIdentity(child.pid, 20, 25);\n        if (this.#childIdentity === null) {\n          throw new Error(\"Supervisor app-server process identity is unavailable\");\n        }\n      }\n      child.on(\"error\", (error) => this.#onChildError(child, error));\n",
    )
    current = _replace_once(
        current,
        "      this.#childTerminationPromise = terminateAppServerChild(\n        child,\n        this.#platformPolicy,\n      );\n",
        "      this.#childTerminationPromise = terminateAppServerChild(\n        child,\n        this.#platformPolicy,\n        undefined,\n        this.#supervisorRuntimeBinding\n          ? {\n              runtimeBinding: this.#supervisorRuntimeBinding,\n              expectedIdentity: this.#childIdentity,\n            }\n          : undefined,\n      );\n",
    )
    current = _replace_once(
        current,
        '      await this.#request(\n        "initialize",\n',
        '      const initializeResult = await this.#request(\n        "initialize",\n',
    )
    current = _replace_once(
        current,
        '      await this.#write({ method: "initialized", params: {} });\n',
        '      if (this.#supervisorRuntimeBinding) {\n'
        '        assertInitializedCodexHome(\n'
        '          this.#supervisorRuntimeBinding,\n'
        '          initializeResult,\n'
        '        );\n'
        '      }\n'
        '      await this.#write({ method: "initialized", params: {} });\n',
    )
    if "readSupervisorRuntimeBinding" not in current:
        raise LcbHardeningError("LCB_RUNTIME_ISOLATION_UNSUPPORTED: lifecycle patch did not apply")

    guard.write_text(
        runtime_path,
        _SUPERVISOR_RUNTIME_TS,
        role="lcb",
    )
    runtime_source = _SUPERVISOR_RUNTIME_TS
    guard.write_text(
        app_server,
        current,
        role="lcb",
    )
    guard.write_text(
        marker,
        json.dumps(
            {
                "contract": LCB_RUNTIME_CONTRACT,
                "hardening_revision": LCB_HARDENING_REVISION,
                "files": ["src/app-server.ts", "src/supervisor-runtime.ts"],
                "files_sha256": {
                    "src/app-server.ts": hashlib.sha256(current.encode("utf-8")).hexdigest(),
                    "src/supervisor-runtime.ts": hashlib.sha256(
                        runtime_source.encode("utf-8")
                    ).hexdigest(),
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        role="lcb",
    )
    return marker


def has_lcb_runtime_hardening(source_root: str | Path) -> bool:
    """Return true only when source and built LCB lifecycle guards are verified."""

    return _marker_is_valid(
        Path(source_root),
        require_build=True,
        path_guard=PhysicalPathGuard(),
    )


def has_lcb_runtime_source_hardening(source_root: str | Path) -> bool:
    """Return true when the source patch is present before the build step."""

    return _marker_is_valid(
        Path(source_root),
        require_build=False,
        path_guard=PhysicalPathGuard(),
    )


def finalize_lcb_runtime_hardening(
    source_root: str | Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> Path:
    """Bind the source marker to the built JavaScript lifecycle implementation."""

    root = Path(source_root)
    guard = path_guard or PhysicalPathGuard()
    guard.verify_root(root, role="lcb", require_directory=True)
    source_files = (
        root / LCB_RUNTIME_MARKER,
        root / "src" / "app-server.ts",
        root / "src" / "supervisor-runtime.ts",
    )
    for path in source_files:
        guard.verify_subpath(path, root, role="lcb")
    if not _marker_is_valid(root, require_build=False, path_guard=guard):
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: source hardening is incomplete"
        )
    built_digests: dict[str, str] = {}
    for relative in LCB_RUNTIME_BUILD_FILES:
        built = root / relative
        guard.verify_subpath(built, root, role="lcb")
        if not built.is_file():
            raise LcbHardeningError(
                f"LCB_RUNTIME_ISOLATION_UNSUPPORTED: built hardening file is missing: {relative}"
            )
        try:
            content = built.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise LcbHardeningError(
                f"LCB_RUNTIME_ISOLATION_UNSUPPORTED: built hardening file is unreadable: {relative}"
            ) from exc
        required = (
            ("assertInitializedCodexHome", "readSupervisorRuntimeBinding", "assertOwnedProcess")
            if relative.endswith("app-server.js")
            else ("captureProcessIdentity", "supervisor-runtime-v1", "csb-lcb-runtime-1")
        )
        if any(token not in content for token in required):
            raise LcbHardeningError(
                f"LCB_RUNTIME_ISOLATION_UNSUPPORTED: built hardening guard is missing: {relative}"
            )
        built_digests[relative] = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = root / LCB_RUNTIME_MARKER
    guard.verify_subpath(marker, root, role="lcb")
    payload = json.loads(marker.read_text(encoding="utf-8"))
    payload["build_files"] = list(LCB_RUNTIME_BUILD_FILES)
    payload["build_files_sha256"] = built_digests
    guard.write_text(
        marker,
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        role="lcb",
    )
    return marker


def require_lcb_runtime_hardening(
    source_root: str | Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> None:
    if not _marker_is_valid(
        Path(source_root),
        require_build=True,
        path_guard=path_guard or PhysicalPathGuard(),
    ):
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: hardened Supervisor lifecycle contract is missing"
        )


def lcb_root_from_entrypoint(entrypoint: str | Path) -> Path:
    """Derive the component root from the actual ``dist/src/index.js`` path."""

    resolved = Path(entrypoint).expanduser().absolute()
    if resolved.name.casefold() != "index.js":
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: launch entrypoint must be dist/src/index.js"
        )
    if resolved.parent.name.casefold() != "src" or resolved.parent.parent.name.casefold() != "dist":
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: launch entrypoint is outside the expected dist/src layout"
        )
    return resolved.parent.parent.parent


def require_lcb_runtime_hardening_from_entrypoint(
    entrypoint: str | Path,
    *,
    path_guard: PhysicalPathGuard | None = None,
) -> Path:
    """Verify the physical launch entrypoint before checking its component marker."""

    guard = path_guard or PhysicalPathGuard()
    resolved_entrypoint = Path(entrypoint).expanduser().absolute()
    guard.verify_root(resolved_entrypoint, role="lcb")
    root = lcb_root_from_entrypoint(resolved_entrypoint)
    guard.verify_root(root, role="lcb", require_directory=True)
    require_lcb_runtime_hardening(root, path_guard=guard)
    return root


def _marker_is_valid(
    root: Path,
    *,
    require_build: bool = False,
    path_guard: PhysicalPathGuard | None = None,
) -> bool:
    marker = root / LCB_RUNTIME_MARKER
    app_server = root / "src" / "app-server.ts"
    runtime = root / "src" / "supervisor-runtime.ts"
    guard = path_guard or PhysicalPathGuard()
    try:
        guard.verify_root(root, role="lcb", require_directory=True)
        for path in (marker, app_server, runtime):
            guard.verify_subpath(path, root, role="lcb")
        payload: Any = json.loads(marker.read_text(encoding="utf-8"))
        app_text = app_server.read_text(encoding="utf-8")
        runtime_text = runtime.read_text(encoding="utf-8")
    except (OSError, UnicodeError, json.JSONDecodeError, PhysicalPathVerificationError):
        return False
    if not isinstance(payload, dict):
        return False
    if (
        payload.get("contract") != LCB_RUNTIME_CONTRACT
        or payload.get("hardening_revision") != LCB_HARDENING_REVISION
        or payload.get("files") != ["src/app-server.ts", "src/supervisor-runtime.ts"]
    ):
        return False
    digests = payload.get("files_sha256")
    source_valid = (
        isinstance(digests, dict)
        and digests.get("src/app-server.ts")
        == hashlib.sha256(app_text.encode("utf-8")).hexdigest()
        and digests.get("src/supervisor-runtime.ts")
        == hashlib.sha256(runtime_text.encode("utf-8")).hexdigest()
        and "readSupervisorRuntimeBinding" in app_text
        and "assertOwnedProcess" in app_text
        and "captureProcessIdentity" in runtime_text
        and "supervisor-runtime-v1" in runtime_text
    )
    if not source_valid or not require_build:
        return source_valid
    build_digests = payload.get("build_files_sha256")
    if payload.get("build_files") != list(LCB_RUNTIME_BUILD_FILES) or not isinstance(
        build_digests, dict
    ):
        return False
    for relative in LCB_RUNTIME_BUILD_FILES:
        built = root / relative
        try:
            guard.verify_subpath(built, root, role="lcb")
            content = built.read_text(encoding="utf-8")
        except (OSError, UnicodeError, PhysicalPathVerificationError):
            return False
        if build_digests.get(relative) != hashlib.sha256(content.encode("utf-8")).hexdigest():
            return False
        required = (
            ("assertInitializedCodexHome", "readSupervisorRuntimeBinding", "assertOwnedProcess")
            if relative.endswith("app-server.js")
            else ("captureProcessIdentity", "supervisor-runtime-v1", "csb-lcb-runtime-1")
        )
        if any(token not in content for token in required):
            return False
    return True


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise LcbHardeningError(
            "LCB_RUNTIME_ISOLATION_UNSUPPORTED: expected upstream lifecycle anchor was not unique"
        )
    return text.replace(old, new, 1)


__all__ = [
    "LCB_HARDENING_REVISION",
    "LCB_RUNTIME_CONTRACT",
    "LCB_RUNTIME_MARKER",
    "LCB_RUNTIME_BUILD_FILES",
    "LcbHardeningError",
    "apply_lcb_runtime_hardening",
    "finalize_lcb_runtime_hardening",
    "has_lcb_runtime_hardening",
    "has_lcb_runtime_source_hardening",
    "lcb_root_from_entrypoint",
    "require_lcb_runtime_hardening",
    "require_lcb_runtime_hardening_from_entrypoint",
]
