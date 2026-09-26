import { Codex } from "@openai/codex-sdk";
import { mkdir, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { planVerifiedRun, type InvocationSource, type VerifiedRunPolicy, type RepositoryBinding } from "./admission.js";
import { verifySnapshot, type snapshotPersona, type Capability } from "./persona.js";
import { startTextResponsesProxy, type TextResponsesHost } from "./responses-proxy.js";
import { runSdkTurn, type Progress } from "./turn.js";
import { startToolServer, type HostTool, type ToolHost } from "./tool-server.js";
import { ToolReceipts } from "./tool-receipts.js";

export interface SessionHost {
  /** Check the live grant/generation before dispatch and before returning output.
   * This is host code, never an instruction or callback supplied by a persona.
   */
  assertCurrent(signal: AbortSignal): Promise<void>;
  model: TextResponsesHost;
  progress(event: Progress): Promise<void>;
  /** Trusted invocation adapter checks durable persona completion evidence.
   * No model response is passed here. Required for every non-report persona. */
  verifyCompletion?(signal: AbortSignal): Promise<true>;
  /** Reviewed invocation adapter; execute must use gateway authorization,
   * durable mutation receipts and host-isolated workspaces. */
  toolBroker?: {
    definitions: readonly HostTool[];
    execute: ToolHost["execute"];
    maxCalls: number;
    repositoryCapabilities: readonly Capability[];
  };
}
export interface AdmittedSession {
  runId: string;
  snapshot: ReturnType<typeof snapshotPersona>;
  policy: VerifiedRunPolicy;
  source: InvocationSource;
  repository?: RepositoryBinding;
  prompt: string;
  /** Additional host-owned model limits from the admitted run grant. */
  maxOutputTokens: number;
  maxResponseBytes: number;
  signal: AbortSignal;
}

/** Official SDK session provisioning shared by invocation adapters.
 *
 * Executable capabilities require an explicit host broker and repository binding
 * where applicable. SDK tools route through MCP and confirmed receipt history.
 * The SDK receives no provider credentials or saved threads; host adapters own
 * privileged operations and their workspace isolation.
 *
 * Returns model output for the persona's completion validator. It does not
 * declare a Task complete, publish an artifact or accept the model's own claims.
 */
export async function runAdmittedSession(input: AdmittedSession, host: SessionHost) {
  const { runId, prompt, maxOutputTokens, maxResponseBytes, signal: callerSignal } = input;
  const snapshot = verifySnapshot(input.snapshot);
  const policy = structuredClone(input.policy);
  const source = structuredClone(input.source);
  const verifyCompletion = host.verifyCompletion?.bind(host);
  const suppliedBroker = host.toolBroker;
  const broker = suppliedBroker ? {
    definitions: Object.freeze(suppliedBroker.definitions.map(tool => Object.freeze({ ...tool, ...(tool.parameters ? { parameters: structuredClone(tool.parameters) } : {}), input: tool.input.strict() }))),
    repositoryCapabilities: Object.freeze([...suppliedBroker.repositoryCapabilities]),
    maxCalls: suppliedBroker.maxCalls, execute: suppliedBroker.execute.bind(suppliedBroker),
  } : undefined;
  const repository = input.repository ? structuredClone(input.repository) : undefined;
  const plan = planVerifiedRun(snapshot, policy, source, repository, broker?.repositoryCapabilities ?? [], Date.now());
  if ((plan.persona.completionPolicy !== "report" && (!broker || !verifyCompletion))
    || plan.capabilities.some(value => value !== "artifacts.publish" && !broker?.definitions.some(tool => tool.capability === value))) {
    throw new Error("Session requires the executable capability broker");
  }
  if (!Number.isSafeInteger(maxOutputTokens) || maxOutputTokens < 1 || maxOutputTokens > 4096
    || !Number.isSafeInteger(maxResponseBytes) || maxResponseBytes < 1 || maxResponseBytes > 65536) {
    throw new Error("Invalid admitted model limits");
  }
  const receipts = broker ? new ToolReceipts(broker.definitions, broker.maxCalls, Math.min(maxResponseBytes, 32768)) : undefined;
  callerSignal.throwIfAborted();
  if (Buffer.byteLength(prompt) + Buffer.byteLength(snapshot.instructions) > plan.limits.maxContextBytes) {
    throw new Error("Task and persona exceed admitted context budget");
  }
  const signal = AbortSignal.any([callerSignal, AbortSignal.timeout(plan.limits.maxDurationMs)]);
  await host.assertCurrent(signal);
  signal.throwIfAborted();
  const root = await mkdtemp(join(tmpdir(), "adp-codex-session-"));
  let proxy: Awaited<ReturnType<typeof startTextResponsesProxy>> | undefined;
  let tools: Awaited<ReturnType<typeof startToolServer>> | undefined;
  try {
    const home = join(root, "home");
    const workspace = join(root, "workspace");
    const codexHome = join(home, ".codex");
    await mkdir(codexHome, { recursive: true, mode: 0o700 });
    await mkdir(workspace, { mode: 0o700 });
    if (broker && receipts) tools = await startToolServer(broker.definitions, {
      assertCurrent: active => host.assertCurrent(active),
      execute: (name, args, active) => receipts.execute({ assertCurrent: signal => host.assertCurrent(signal),
        execute: (...args) => broker.execute(...args) }, name, args, active),
    }, { capabilities: plan.capabilities, maxCalls: broker.maxCalls, maxRequestBytes: 63 * 1024,
      maxResultBytes: maxResponseBytes, timeoutMs: Math.min(plan.limits.maxDurationMs, 120000), signal });
    proxy = await startTextResponsesProxy(async (request, requestSignal) => {
      const active = AbortSignal.any([signal, requestSignal]);
      active.throwIfAborted();
      await host.assertCurrent(active);
      active.throwIfAborted();
      return host.model(request, active);
    }, {
      ...(broker && receipts ? { tools: { definitions: broker.definitions, validateHistory: history => receipts.validateHistory(history),
        acceptResponse: response => { receipts.acceptModelResponse(response); return true; } } } : {}),
      model: policy.canonicalModel, effort: plan.persona.effort,
      maxOutputTokens: maxOutputTokens,
      // Reserve wrapper space within the existing 64 KiB Task IPC contract.
      maxRequestBytes: 63 * 1024, maxResponseBytes: maxResponseBytes,
      maxOperations: plan.limits.maxTurns, timeoutMs: Math.min(plan.limits.maxDurationMs, 120000),
    });
    const codex = new Codex({
      config: { ...plan.sdkConfig, developer_instructions: snapshot.instructions,
        ...(tools && broker ? { mcp_servers: { adp: { url: tools.url, bearer_token_env_var: "ADP_TOOL_SESSION_TOKEN",
          required: true, enabled_tools: broker.definitions.map(tool => tool.name), startup_timeout_sec: 10,
          // Gateway admission and per-effect checks own approval for these exact
          // host tools. Do not falsely mark write tools as read-only to run headlessly.
          tools: Object.fromEntries(broker.definitions.map(tool => [tool.name, { approval_mode: "approve" }])),
          tool_timeout_sec: Math.min(120, Math.ceil(plan.limits.maxDurationMs / 1000)) } } } : {}) },
      baseUrl: proxy.baseUrl, apiKey: proxy.token,
      // An allowlist, not a copy of process.env. In particular inherited
      // BG_CONFIG_DIR, tokens, AWS roles, CODEX_HOME and HOME do not propagate.
      env: {
        PATH: process.env.PATH ?? "/usr/local/bin:/usr/bin:/bin",
        HOME: home, CODEX_HOME: codexHome, TMPDIR: root,
        ...(tools ? { ADP_TOOL_SESSION_TOKEN: tools.token } : {}),
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
    if (plan.persona.completionPolicy !== "report" && await verifyCompletion!(signal) !== true) throw new Error("Persona completion evidence was not verified");
    await host.assertCurrent(signal);
    signal.throwIfAborted();
    return evidence;
  } catch (error) {
    throw proxy?.failure ?? error;
  } finally {
    try { await proxy?.close(); }
    finally {
      try { await tools?.close(); }
      finally { await rm(root, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 }); }
    }
  }
}
