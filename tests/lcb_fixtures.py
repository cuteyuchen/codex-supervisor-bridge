from __future__ import annotations

from pathlib import Path

from codex_supervisor_bridge.bootstrap.lcb_hardening import apply_lcb_runtime_hardening

UPSTREAM_LCB_APP_SERVER_SOURCE = '''import { platformPolicyFor, type PlatformPolicy } from "./platform.js";

export async function terminateAppServerChild(
  child: ChildProcessWithoutNullStreams,
  platformPolicy: PlatformPolicy,
  timeouts: ChildTerminationTimeouts = DEFAULT_CHILD_TERMINATION_TIMEOUTS,
): Promise<void> {
  if (platformPolicy.hasChildExited(child)) {
    return;
  }

  let terminationError: Error | undefined;
  try {
    child.stdin.end();
  } catch (error) {
    terminationError = error instanceof Error ? error : new Error(String(error));
  }

  try {
    platformPolicy.softTerminateChild(child);
  } catch (error) {
    terminationError = error instanceof Error ? error : new Error(String(error));
  }

  try {
    platformPolicy.hardTerminateChild(child);
  } catch (error) {
    terminationError = error instanceof Error ? error : new Error(String(error));
  }
}

export function sanitizedChildEnvironment(): NodeJS.ProcessEnv {
  const childEnvironment = { ...process.env };
  delete childEnvironment.CONTROL_PLANE_API_KEY;
  return childEnvironment;
}

export class CodexAppServerManager {
  #stdoutBuffer = Buffer.alloc(0);

  constructor(options: AppServerOptions) {
    const sourceEnvironment = options.environment ?? process.env;
    this.#platformPolicy = options.platformPolicy ?? platformPolicyFor();
    this.#executable = options.executable ?? resolveCodexExecutable(sourceEnvironment);
  }

  async startChild(): Promise<void> {
    const child = spawn(this.#executable, ["app-server", "--listen", "stdio://"], {});
    await new Promise<void>((resolve, reject) => {
      const onSpawn = (): void => resolve();
      const onError = (error: Error): void => reject(error);
        child.once("spawn", onSpawn);
        child.once("error", onError);
      });
      child.on("error", (error) => this.#onChildError(child, error));
  }

  #terminateChild(child: ChildProcessWithoutNullStreams): Promise<void> {
    if (!this.#childTerminationPromise) {
      this.#childTerminationPromise = terminateAppServerChild(
        child,
        this.#platformPolicy,
      );
    }
    return this.#childTerminationPromise;
  }
}
'''


def write_upstream_lcb_repository(root: Path, *, entrypoint: bool = True) -> Path:
    source = root / "src" / "app-server.ts"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(UPSTREAM_LCB_APP_SERVER_SOURCE, encoding="utf-8", newline="\n")
    if entrypoint:
        built = root / "dist" / "src" / "index.js"
        built.parent.mkdir(parents=True, exist_ok=True)
        built.write_text("// fake Local-Codex-Bridge entrypoint\n", encoding="utf-8")
    return root


def write_hardened_lcb_repository(root: Path, *, entrypoint: bool = True) -> Path:
    write_upstream_lcb_repository(root, entrypoint=entrypoint)
    apply_lcb_runtime_hardening(root)
    return root
