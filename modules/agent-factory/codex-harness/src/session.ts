import { Codex } from "@openai/codex-sdk";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { planVerifiedRun, type InvocationSource, type VerifiedRunPolicy } from "./admission.js";
import { verifySnapshot, type snapshotPersona } from "./persona.js";
import { startTextResponsesProxy, type TextResponsesHost } from "./responses-proxy.js";
import { runSdkTurn, type Progress } from "./turn.js";

export interface SessionHost {
  /** Check the live grant/generation before dispatch and before returning output.
   * This is host code, never an instruction or callback supplied by a persona.
   */
  assertCurrent(signal: AbortSignal): Promise<void>;
  model: TextResponsesHost;
  progress(event: Progress): Promise<void>;
}
export interface AdmittedSession {
  runId: string;
  snapshot: ReturnType<typeof snapshotPersona>;
  policy: VerifiedRunPolicy;
  source: InvocationSource;
  prompt: string;
  /** Additional host-owned model limits from the admitted run grant. */
  maxOutputTokens: number;
  maxResponseBytes: number;
  signal: AbortSignal;
}

/** Official SDK session provisioning shared by invocation adapters.
 *
 * This report-only stage has no model-visible executable tools. It admits no
 * repository/workspace binding and never inherits provider credentials, Codex
 * settings or saved threads. Tool-bearing personas require the capability broker
 * and filesystem isolation stage; their capabilities are refused here.
 *
 * Returns model output for the persona's completion validator. It does not
 * declare a Task complete, publish an artifact or accept the model's own claims.
 */
export async function runAdmittedSession(input: AdmittedSession, host: SessionHost) {
  const { runId, prompt, maxOutputTokens, maxResponseBytes, signal: callerSignal } = input;
  const snapshot = verifySnapshot(input.snapshot);
  const policy = structuredClone(input.policy);
  const source = structuredClone(input.source);
  const plan = planVerifiedRun(snapshot, policy, source, undefined, [], Date.now());
  if (plan.persona.completionPolicy !== "report" || plan.capabilities.some(value => value !== "artifacts.publish")) {
    throw new Error("Session requires the executable capability broker");
  }
  if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens < 1 || maxOutputTokens > 4096
    || !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1 || maxResponseBytes > 65536) {
    throw new Error("Invalid admitted model limits");
  }
  callerSignal.throwIfAborted();
  if (Buffer.byteLength(prompt) + Buffer.byteLength(snapshot.instructions) > plan.limits.maxContextBytes) {
    throw new Error("Task and persona exceed admitted context budget");
  }
  const signal = AbortSignal.any([callerSignal, AbortSignal.timeout(plan.limits.maxDurationMs)]);
  await host.assertCurrent(signal);
  signal.throwIfAborted();
  const root = await mkdtemp(join(tmpdir(), "adp-codex-session-"));
  let proxy: Awaited<ReturnType<typeof startTextResponsesProxy>> | undefined;
  try {
    const home = join(root, "home");
    const workspace = join(root, "workspace");
    const codexHome = join(home, ".codex");
    await mkdir(codexHome, { recursive: true, mode: 0o700 });
    await mkdir(workspace, { mode: 0o700 });
    proxy = await startTextResponsesProxy(async (request, requestSignal) => {
      const active = AbortSignal.any([signal, requestSignal]);
      active.throwIfAborted();
      await host.assertCurrent(active);
      active.throwIfAborted();
      return host.model(request, active);
    }, {
      model: policy.canonicalModel, effort: plan.persona.effort,
      maxOutputTokens: maxOutputTokens,
      // Reserve wrapper space within the existing 64 KiB Task IPC contract.
      maxRequestBytes: 63 * 1024, maxResponseBytes: maxResponseBytes,
      maxOperations: plan.limits.maxTurns, timeoutMs: Math.min(plan.limits.maxDurationMs, 120000),
    });
    const codex = new Codex({
      config: { ...plan.sdkConfig, developer_instructions: snapshot.instructions },
      baseUrl: proxy.baseUrl, apiKey: proxy.token,
      // An allowlist, not a copy of process.env. In particular inherited
      // BG_CONFIG_DIR, tokens, AWS roles, CODEX_HOME and HOME do not propagate.
      env: {
        PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
        HOME: home, CODEX_HOME: codexHome, TMPDIR: root,
        XDG_CONFIG_HOME: join(home, ".config"), XDG_CACHE_HOME: join(home, ".cache"),
        XDG_DATA_HOME: join(home, ".local", "share"), XDG_STATE_HOME: join(home, ".local", "state"),
        GIT_CONFIG_NOSYSTEM: "1", GIT_CONFIG_GLOBAL: "/dev/null",
      },
    });
    const evidence = await runSdkTurn(codex.startThread({ ...plan.options, workingDirectory: workspace, skipGitRepoCheck: true }), prompt, {
      runId: runId, personaKey: plan.persona.key, model: policy.canonicalModel,
      harnessRevision: policy.harnessRevision, surface: source.kind,
      timeoutMs: plan.limits.maxDurationMs, maxInputBytes: plan.limits.maxContextBytes,
      maxOutputBytes: maxResponseBytes, signal,
    }, event => host.progress(event));
    await host.assertCurrent(signal);
    signal.throwIfAborted();
    return evidence;
  } finally {
    try { await proxy?.close(); }
    finally { await rm(root, { recursive: true, force: true }); }
  }
}
