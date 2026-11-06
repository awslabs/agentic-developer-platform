import { execFile as runFile, spawn, type ChildProcess } from 'node:child_process';
import { promisify } from 'node:util';
import { setTimeout as sleep } from 'node:timers/promises';
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, realpathSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { statSync } from 'node:fs';
import { fromInstanceMetadata } from '@smithy/credential-provider-imds';
import { z } from 'zod';
import { startCrewModelAdapter } from './crew-model-adapter';
import { HOST_MODEL_REQUEST_LIMITS } from './gateway/chat-model-contract';
import { crewHostModelClient } from './crew-host-model-client';
import { clearToolsQuarantine, ensureGhWrapperFirst, renderCrewToolsServer } from './crew-kirocrew';
import { crewHostToolsServer, type CrewToolsServer } from './crew-tools';
import { currentCrewHostAuthority, readTrustedJson } from './crew-host-trusted';
import { writeCrewHomeCodexConfig, type CrewCodexSettings } from './crew-home-codex-config';
import { writeCrewHomeGuidance, type CrewGuidanceSettings } from './crew-home-guidance';

const modelConfigSchema = z.object({
  run_id: z.string().uuid(), lease_generation: z.number().int().positive(),
  gateway_execute_arn: z.string().regex(/^arn:aws:execute-api:[a-z0-9-]+:[0-9]{12}:[a-z0-9]+\/[A-Za-z0-9_-]+$/),
  model_id: z.string().regex(/^[A-Za-z0-9_.:-]{1,256}$/),
}).strict();

export function currentCrewHostModel(configFile = '/run/adp/crew-model.json',
  authority = currentCrewHostAuthority, trustedUid = 0) {
  const config = modelConfigSchema.parse(readTrustedJson(configFile, trustedUid));
  const run = authority();
  if (run.runId !== config.run_id || run.generation !== config.lease_generation) {
    throw new Error('Crew host model configuration is stale');
  }
  const [, , , region, , apiAndStage] = config.gateway_execute_arn.split(':');
  const [api, stage] = apiAndStage.split('/');
  return { runId: run.runId, modelId: config.model_id,
    region, gatewayUrl: `https://${api}.execute-api.${region}.amazonaws.com/${stage}` };
}

export function currentCrewModelToken(runId: string, generation: number,
  tokenFile = '/run/adp/crew-host/model-token/token.json', now = Math.floor(Date.now() / 1000), trustedUid = 0) {
  return readCrewModelToken(runId, generation, tokenFile, now, trustedUid).token;
}

/** Unix seconds the delivered model token expires; same trust checks as {@link currentCrewModelToken}. */
export function currentCrewModelTokenExpiry(runId: string, generation: number,
  tokenFile = '/run/adp/crew-host/model-token/token.json', now = Math.floor(Date.now() / 1000), trustedUid = 0) {
  return readCrewModelToken(runId, generation, tokenFile, now, trustedUid).expires_at;
}

function readCrewModelToken(runId: string, generation: number, tokenFile: string, now: number, trustedUid: number) {
  const directory = statSync(dirname(tokenFile));
  if (!directory.isDirectory() || directory.uid !== trustedUid || (directory.mode & 0o007) !== 0) {
    throw new Error('Untrusted Crew model token directory');
  }
  const handle = openSync(tokenFile, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = fstatSync(handle);
    if (!info.isFile() || info.uid !== process.getuid?.() || (info.mode & 0o077) !== 0) {
      throw new Error('Untrusted Crew model token file');
    }
    const value = z.object({ run_id: z.string().uuid(), lease_generation: z.number().int().positive(),
      token: z.string().regex(/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$/).max(2048),
      expires_at: z.number().int().positive() }).strict().parse(JSON.parse(readFileSync(handle, 'utf8')));
    if (value.run_id !== runId || value.lease_generation !== generation || value.expires_at <= now) {
      throw new Error('Crew model token expired or replaced');
    }
    return value;
  } finally { closeSync(handle); }
}

export function verifyCrewHostModelProfile(home: string, modelId: string) {
  if (lstatSync(home).isSymbolicLink() || realpathSync(join(home, '.kiro', 'agents')) !==
      join(realpathSync(home), '.kiro', 'agents')) throw new Error('Untrusted Crew model profile path');
  const read = (path: string) => {
    const handle = openSync(path, constants.O_RDONLY | constants.O_NOFOLLOW);
    try {
      if (!fstatSync(handle).isFile()) throw new Error('Crew model profile is not a file');
      return JSON.parse(readFileSync(handle, 'utf8')) as unknown;
    } finally { closeSync(handle); }
  };
  const config = z.object({
    agent: z.object({ acp_backend: z.literal('claude'), member_acp_backend: z.literal('claude'),
      model: z.literal(modelId) }).passthrough(),
    memory: z.object({ embed_model_path: z.literal('/opt/adp/crew/current/models/qwen3-embedding-0.6b-q8_0.gguf') }).passthrough(),
  }).passthrough().parse(read(join(home, 'config.json')));
  const agent = z.object({ name: z.literal('kirocrew'), model: z.literal(config.agent.model) }).passthrough()
    .parse(read(join(home, '.kiro', 'agents', 'kirocrew.json')));
  return agent.model;
}

/**
 * What Claude Code's `/model opus|sonnet|haiku` resolve to on a Dev Box (dev 2026-10-25).
 *
 * Claude Code names models the Anthropic way (`claude-opus-5-5`), which the gateway does not know;
 * with these variables set it sends the gateway's inference-profile ids instead, exactly what the
 * laptop launcher's Bedrock mode sends. The gateway still decides entitlement per owner. The run's
 * model stays the default (ANTHROPIC_MODEL); these only steer the family aliases.
 */
export const CREW_MODEL_FAMILY_DEFAULTS = {
  opus: 'global.anthropic.claude-opus-5-5',
  sonnet: 'global.anthropic.claude-sonnet-5',
  haiku: 'global.anthropic.claude-haiku-4-5-20251001-v1:0',
} as const;

/**
 * Tools catalogue warm-up before Kiro Crew starts (#8835).
 *
 * Kiro probes the adp-tools MCP server as soon as a session needs it and quarantines it after three
 * 15 s timeouts; on dev the first listing through the host route took 15.5–16.3 s (gens 51–52). The
 * adapter remembers the catalogue, so a listing that already happened makes the probe a memory read.
 * The listing cannot succeed until the controller has bound this host (gen 52: 403 at boot, bound
 * ~25 s later), so the warm-up keeps retrying every TOOLS_WARMUP_RETRY_MS until one listing is
 * answered 200 or TOOLS_WARMUP_GIVE_UP_MS have passed. Kiro's start waits at most
 * TOOLS_WARMUP_WAIT_MS for it (its `kirocrew token` backs the dashboard mint); a probe that arrives
 * while a listing is in flight joins that same upstream call.
 */
export const TOOLS_WARMUP_WAIT_MS = 10_000;
export const TOOLS_WARMUP_RETRY_MS = 5_000;
export const TOOLS_WARMUP_GIVE_UP_MS = 300_000;

/** Run the warm-up loop, wait at most `waitMs` for it, journal first failure, success and abandonment; never throws. */
export async function warmCrewTools(warm: () => Promise<number>, report: (line: string) => void,
  options: { waitMs?: number; retryMs?: number; giveUpMs?: number; signal?: AbortSignal } = {}): Promise<void> {
  const { waitMs = TOOLS_WARMUP_WAIT_MS, retryMs = TOOLS_WARMUP_RETRY_MS, giveUpMs = TOOLS_WARMUP_GIVE_UP_MS, signal } = options;
  const startedAt = Date.now();
  let settled = false;
  const loop = (async () => {
    for (let attempt = 1; ; attempt++) {
      let outcome: { status: number } | { error: string };
      try { outcome = { status: await warm() }; }
      catch (error) { outcome = { error: error instanceof Error ? error.message.slice(0, 200) : 'unknown' }; }
      if ('status' in outcome && outcome.status === 200) {
        report(JSON.stringify({ event: 'crew_tools_warmup', status: 200, attempt, elapsed_ms: Date.now() - startedAt }));
        return;
      }
      if (attempt === 1) report(JSON.stringify({ event: 'crew_tools_warmup', ...outcome, attempt, elapsed_ms: Date.now() - startedAt }));
      if (signal?.aborted) return;
      if (Date.now() - startedAt + retryMs >= giveUpMs) {
        report(JSON.stringify({ event: 'crew_tools_warmup_abandoned', ...outcome, attempts: attempt, elapsed_ms: Date.now() - startedAt }));
        return;
      }
      try { await sleep(retryMs, undefined, { signal, ref: false }); } catch { return; }
    }
  })().then(() => { settled = true; });
  let timer: NodeJS.Timeout | undefined;
  await Promise.race([loop, new Promise<void>(resolve => { timer = setTimeout(resolve, waitMs); timer.unref(); })]);
  if (timer) clearTimeout(timer);
  if (!settled) report(JSON.stringify({ event: 'crew_tools_warmup_pending', waited_ms: waitMs }));
}

export async function startCrewHostModel(options: {
  config?: () => ReturnType<typeof currentCrewHostModel>;
  credentials?: ReturnType<typeof fromInstanceMetadata>;
  modelToken?: () => Promise<string>;
  launch?: (environment: NodeJS.ProcessEnv, args: string[]) => ChildProcess;
  send?: typeof fetch;
  verifyProfile?: (home: string, modelId: string) => string;
  renderTools?: (home: string, shim: CrewToolsServer) => Promise<void>;
  ensureGhWrapper?: (home: string) => Promise<void>;
  publishShell?: (payload: { run_id: string; model_id: string; base_url: string; secret: string;
    model_aliases: typeof CREW_MODEL_FAMILY_DEFAULTS }) => Promise<void>;
  removeShell?: () => Promise<void>;
  probeBackend?: typeof probeClaudeBackend;
  warmTools?: () => Promise<number>;
  warmupWaitMs?: number;
  clearQuarantine?: (home: string) => Promise<void>;
  seedSkills?: (home: string) => Promise<void>;
  codexConfig?: (home: string, settings: CrewCodexSettings) => Promise<unknown>;
  guidance?: (home: string, settings: CrewGuidanceSettings) => Promise<unknown>;
  refreshAws?: (environment: NodeJS.ProcessEnv) => Promise<void>;
  report?: (line: string) => void;
} = {}) {
  const config = options.config ?? currentCrewHostModel;
  const initial = config();
  (options.verifyProfile ?? verifyCrewHostModelProfile)('/mnt/crew', initial.modelId);
  const currentRun = () => {
    const current = config();
    if (current.runId !== initial.runId || current.modelId !== initial.modelId ||
        current.gatewayUrl !== initial.gatewayUrl) throw new Error('Crew host model configuration changed');
    return current.runId;
  };
  // IMDS answers slowly under load; one slow answer must not fail a turn (#8223 cause 48: a local
  // credential failure surfaced to Claude Code as 403 and latched the backend as signed out).
  const credentials = options.credentials ?? fromInstanceMetadata({ maxRetries: 2, timeout: 3000 });
  // Both readers are `async` so a refused or expired token file *rejects* instead of throwing
  // synchronously: `() => Promise.resolve(read())` evaluated `read()` before any promise existed,
  // so the route's `.catch` never saw the throw. On dev 2026-10-22 (#8223 cause 55) every model
  // call failed locally in 5 ms once the pushed 60 s token expired, because the freshness check
  // that should have fallen back to a refresh threw instead and the refresh never ran. Note the
  // gateway pushes a token only on the owner's handoff and on host-initiated refreshes, never at
  // boot: Kiro Crew must start at once (its `kirocrew token` backs the dashboard mint — dev
  // 2026-10-22 18:16, a 60 s wait here made "Open my EC2 Crew" fail with crew_mint_unavailable),
  // and the first call's refresh is what delivers the first token.
  const modelToken = async () => currentCrewModelToken(currentRun(), currentCrewHostAuthority().generation);
  // Lets a host call skip the refresh round trip while the pushed token is still fresh (#8223 Dev Box tool latency).
  const modelTokenExpiresAt = async () => currentCrewModelTokenExpiry(currentRun(), currentCrewHostAuthority().generation);
  // Claude Code's requests (system prompt + tool schemas + a long conversation) need the host envelope,
  // not the chat sandbox's 64 KiB one (#8223 cause 36).
  const hostCancellation = new AbortController();
  const adapter = await startCrewModelAdapter(crewHostModelClient({ ...initial, currentRun, credentials,
    modelToken: options.modelToken ?? modelToken, modelTokenExpiresAt: options.modelToken ? undefined : modelTokenExpiresAt,
    send: options.send }, hostCancellation.signal), initial.modelId, hostCancellation.signal, true, HOST_MODEL_REQUEST_LIMITS);
  let shellPublished = false;
  let awsTimer: NodeJS.Timeout | undefined;
  try {
    currentRun();
    // #8380: Kiro Crew and Claude Code reach ADP tools through this adapter; register the shim
    // now that the adapter's address and secret exist for this boot and this run.
    await (options.renderTools ?? renderCrewToolsServer)('/mnt/crew', crewHostToolsServer(adapter, initial.runId));
    // #8748: an agent-installed ~/.local/bin/gh must not shadow the bundle wrapper on Crew's PATH.
    await (options.ensureGhWrapper ?? (home => ensureGhWrapperFirst(home, undefined,
      options.report ?? (text => process.stderr.write(`${text}\n`)))))('/mnt/crew');
    currentRun();
    const report = options.report ?? ((text: string) => { process.stderr.write(`${text}\n`); });
    // #8835: a quarantine verdict on the retained home is history, not evidence; never fatal.
    try { await (options.clearQuarantine ?? (home => clearToolsQuarantine(home, report)))('/mnt/crew'); }
    catch (error) { report(JSON.stringify({ event: 'crew_tools_quarantine_unreadable', message: error instanceof Error ? error.message.slice(0, 200) : 'unknown' })); }
    // #9589: a dashboard session runs Claude Code and Codex as this user in this home, not in the
    // owner shell, so the bundle's ADP skills (adp-flow, adp-grilling) are seeded here too. Same
    // installer and provenance markers as the owner shell (`adp claude setup --skills-only`); a
    // user-owned or modified skill directory is left alone. Advisory: a seeding failure is journaled,
    // Kiro Crew still starts.
    try { await (options.seedSkills ?? (home => seedCrewHomeSkills(home)))('/mnt/crew'); }
    catch (error) { report(JSON.stringify({ event: 'crew_home_skills_unseeded', message: error instanceof Error ? error.message.slice(0, 200) : 'unknown' })); }
    // #9628: Claude Code is configured by the ANTHROPIC_* environment below; Codex needs a provider in
    // this home's ~/.codex/config.toml (the owner shell's codex() function never applies here). The
    // managed block carries the same adp-shell provider the owner shell uses: this boot's loopback
    // adapter, the published model, no web search, the adp-tools MCP server. Advisory like the skills.
    try { await (options.codexConfig ?? ((home, settings) => writeCrewHomeCodexConfig(home, settings, report)))('/mnt/crew', { modelId: initial.modelId, baseUrl: adapter.baseUrl }); }
    catch (error) { report(JSON.stringify({ event: 'crew_home_codex_unconfigured', message: error instanceof Error ? error.message.slice(0, 200) : 'unknown' })); }
    // #9655: the facts an agent in this home needs and nothing on the box used to state: the retained
    // workspace, the organisation's commit-reference rule, the owner's AWS connections through the
    // SDK. One managed block in ~/.claude/CLAUDE.md and ~/.codex/AGENTS.md; the user's own text is
    // kept. Advisory like the skills and the Codex provider.
    try { await (options.guidance ?? ((home, settings) => writeCrewHomeGuidance(home, settings, report)))('/mnt/crew', { region: initial.region }); }
    catch (error) { report(JSON.stringify({ event: 'crew_home_guidance_unwritten', message: error instanceof Error ? error.message.slice(0, 200) : 'unknown' })); }
    // #8835: list the tools now (and keep trying until the host is bound) so Kiro Crew's first MCP
    // probe answers from the adapter's memory.
    await warmCrewTools(options.warmTools ?? adapter.warmTools, report, { waitMs: options.warmupWaitMs, signal: hostCancellation.signal });
    currentRun();
    shellPublished = true;
    await (options.publishShell ?? publishOwnerShellModel)({ run_id: initial.runId, model_id: initial.modelId,
      base_url: adapter.baseUrl, secret: adapter.secret, model_aliases: CREW_MODEL_FAMILY_DEFAULTS });
    currentRun();
    const environment: NodeJS.ProcessEnv = {
      HOME: '/mnt/crew', KIROCREW_HOME: '/mnt/crew',
      AWS_CONFIG_FILE: '/run/adp-crew-aws/config', AWS_EC2_METADATA_DISABLED: 'true', AWS_REGION: initial.region,
      AWS_SHARED_CREDENTIALS_FILE: '/dev/null',
      // host/ first: the Claude agent adapter is a `#!/usr/bin/env node` script and the guest has no
      // system Node (#8223 cause 31) — without it every ACP session died at exec and turns hung.
      // host/adp right after host/: a dashboard session's Claude Code runs the box CLI (`adp …`) the
      // way the owner shell does (#9589); the three ADP_CREW_* values below are what `adp` reads for
      // host-run mode (loopback adapter URL, its secret, this host run), exactly as the owner shell's
      // model.env and the ADP tools shim receive them.
      PATH: `/opt/adp/crew/current/host:${CREW_HOST_ADP_DIR}:/opt/adp/crew/current/clients/node_modules/.bin:/opt/adp/crew/current/venv/bin:/usr/local/bin:/usr/bin:/bin`,
      ADP_CREW_TOOLS_URL: adapter.baseUrl, ADP_CREW_TOOLS_TOKEN: adapter.secret, ADP_CREW_RUN_ID: initial.runId,
      CLAUDE_AGENT_ACP_BIN: '/opt/adp/crew/current/clients/node_modules/.bin/claude-agent-acp',
      CLAUDE_CODE_EXECUTABLE: '/opt/adp/crew/current/clients/node_modules/.bin/claude',
      ANTHROPIC_BASE_URL: adapter.baseUrl, ANTHROPIC_API_KEY: adapter.secret, ANTHROPIC_MODEL: initial.modelId,
      // #9628: the Codex provider written into ~/.codex/config.toml names this variable (env_key).
      OPENAI_API_KEY: adapter.secret,
      ANTHROPIC_DEFAULT_OPUS_MODEL: CREW_MODEL_FAMILY_DEFAULTS.opus, ANTHROPIC_DEFAULT_SONNET_MODEL: CREW_MODEL_FAMILY_DEFAULTS.sonnet,
      ANTHROPIC_DEFAULT_HAIKU_MODEL: CREW_MODEL_FAMILY_DEFAULTS.haiku,
      CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1',
    };
    let syncingAws = false;
    const refreshAws = async () => {
      if (syncingAws || hostCancellation.signal.aborted) return;
      syncingAws = true;
      try { await (options.refreshAws ?? refreshOwnerAwsProfiles)(environment); }
      catch { report('Dev Box AWS profiles unavailable; retrying connection discovery'); }
      finally { syncingAws = false; }
    };
    await refreshAws();
    currentRun();
    awsTimer = setInterval(() => { void refreshAws(); }, 60_000);
    awsTimer.unref();
    // Name the Claude backend's state in the journal before Kiro Crew starts (dev 2026-10-20:
    // the dashboard offered "set up Kiro CLI" and nothing on the host said whether the Claude
    // adapter and CLI it is configured to use were even runnable). Advisory: a failed probe is
    // logged, never fatal, because the ACP attempt itself is the authority for a turn.
    // It runs alongside Kiro's start, not before it: on a fresh box the two `--version` calls took
    // 22 s off a cold disk (dev gen 52, 2026-10-26) and the owner's first handoff reached a Kiro that
    // had not started yet (503 after 21 s).
    const probe = (options.probeBackend ?? probeClaudeBackend)(environment)
      .then(lines => lines.forEach(line => report(line)),
        (error: unknown) => report(`Crew host model: backend probe failed (${error instanceof Error ? error.message.slice(0, 120) : 'unknown'})`));
    const args = ['gateway', '--no-open', '--no-tunnel', '--no-crons', '--port', '5476'];
    const child = options.launch?.(environment, args) ?? spawn('/opt/adp/crew/current/venv/bin/kirocrew',
      args,
      { cwd: '/mnt/crew', env: environment, stdio: 'ignore' });
    await probe;
    let closed = false;
    return { child, adapter, close: async () => {
      if (closed) return;
      closed = true;
      hostCancellation.abort();
      clearInterval(awsTimer);
      adapter.suspend();
      child.kill('SIGTERM');
      try { await (options.removeShell ?? removeOwnerShellModel)(); }
      finally { await adapter.close(); }
    } };
  } catch (error) {
    clearInterval(awsTimer);
    hostCancellation.abort();
    adapter.suspend();
    try { if (shellPublished) await (options.removeShell ?? removeOwnerShellModel)(); }
    finally { await adapter.close(); }
    throw error;
  }
}

async function refreshOwnerAwsProfiles(environment: NodeJS.ProcessEnv): Promise<void> {
  await promisify(runFile)('/opt/adp/crew/current/host/adp-cred', ['sync'],
    { env: environment, timeout: 45_000, maxBuffer: 4096 });
}

const shellPublisher = '/opt/adp/crew/publish_shell_model.py';

function runShellPublisher(input?: object): Promise<void> {
  return new Promise((resolve, reject) => {
    const child = spawn('/usr/bin/sudo', ['-n', '/usr/bin/python3', '-I', shellPublisher,
      ...(input ? [] : ['--remove'])], { stdio: ['pipe', 'ignore', 'ignore'] });
    child.stdin.on('error', () => undefined);
    child.stdin.end(input ? JSON.stringify(input) : '');
    child.on('error', () => reject(new Error('Dev Box shell model publisher unavailable')));
    child.on('close', code => code === 0 ? resolve() : reject(new Error('Dev Box shell model publication refused')));
  });
}

function publishOwnerShellModel(payload: { run_id: string; model_id: string; base_url: string; secret: string;
  model_aliases: typeof CREW_MODEL_FAMILY_DEFAULTS }): Promise<void> {
  return runShellPublisher(payload);
}

function removeOwnerShellModel(): Promise<void> {
  return runShellPublisher();
}

const execFile = promisify(runFile);

/**
 * One journal line per Claude backend component Kiro Crew is configured to spawn.
 *
 * Kiro Crew's ``agent.acp_backend = claude`` drives Claude Code through the adapter named by
 * ``CLAUDE_AGENT_ACP_BIN``, which in turn runs the CLI named by ``CLAUDE_CODE_EXECUTABLE`` in the
 * Agent SDK's non-interactive stream-json mode (no first-run prompts). Both are shipped in the
 * bundle; if either fails to run under the Crew environment the dashboard only shows Kiro Crew's
 * generic prerequisite card, so this names the real state. ``--version`` costs no model call.
 * Output is bounded and never includes the adapter secret (``--version`` prints a version).
 */
export const CREW_HOST_ADP_DIR = '/opt/adp/crew/current/host/adp';
const CREW_HOME_SKILL_CLIENTS = ['claude-code', 'codex'] as const;

/**
 * Install the bundle's ADP skills into the Crew home for both clients (#9589).
 *
 * The owner shell gets them from `prepare_shell.sh` (`adp claude setup --skills-only`); a dashboard
 * session runs as the Crew agent user in `/mnt/crew`, which that path never touches. Same installer
 * (`adp_skills.py`), same source (`host/adp/skills`, the archive the bundle verified) and the same
 * provenance markers, so re-seeding on every start is idempotent and a modified or user-owned skill
 * directory is preserved with a warning rather than replaced.
 */
export async function seedCrewHomeSkills(home: string,
  runner: (file: string, args: string[], options: { timeout: number; maxBuffer: number }) => Promise<unknown> = execFile): Promise<void> {
  for (const client of CREW_HOME_SKILL_CLIENTS) {
    await runner('/opt/adp/crew/current/venv/bin/python3', ['-I', `${CREW_HOST_ADP_DIR}/adp_skills.py`,
      '--client', client, '--source', `${CREW_HOST_ADP_DIR}/skills`, '--home', home], { timeout: 30_000, maxBuffer: 16_384 });
  }
}

export async function probeClaudeBackend(environment: NodeJS.ProcessEnv,
  runner: (file: string, args: string[], env: NodeJS.ProcessEnv) => Promise<string> = defaultProbeRunner,
): Promise<string[]> {
  const components: Array<[string, string | undefined]> = [
    ['claude', environment.CLAUDE_CODE_EXECUTABLE], ['claude-agent-acp', environment.CLAUDE_AGENT_ACP_BIN]];
  const lines: string[] = [];
  for (const [name, file] of components) {
    if (!file) { lines.push(`Crew host model: ${name} not configured`); continue; }
    try {
      const output = (await runner(file, ['--version'], environment)).trim().split('\n')[0]?.slice(0, 120) ?? '';
      lines.push(`Crew host model: ${name} --version -> ${output || 'ok'}`);
    } catch (error) {
      const reason = error instanceof Error && error.message ? error.message.replace(/\s+/g, ' ').slice(0, 160) : 'failed';
      lines.push(`Crew host model: ${name} --version failed (${reason})`);
    }
  }
  return lines;
}

async function defaultProbeRunner(file: string, args: string[], env: NodeJS.ProcessEnv): Promise<string> {
  const { stdout, stderr } = await execFile(file, args, { env, cwd: env.HOME ?? '/mnt/crew', timeout: 20_000, maxBuffer: 4096 });
  return stdout || stderr;
}

/** One stderr line naming why the service stopped; messages are fixed constants, never secrets. */
export function describeHostModelFailure(error: unknown): string {
  const name = error instanceof Error ? error.name || error.constructor.name : 'Error';
  const message = error instanceof Error && error.message ? `: ${error.message.replace(/\s+/g, ' ').slice(0, 300)}` : '';
  return `Crew host model refused: ${name}${message}`;
}

if (require.main === module) {
  startCrewHostModel().then(({ child, close }) => {
    child.on('exit', (code, signal) => {
      process.stderr.write(`Crew host model: Kiro Crew exited (code ${code ?? 'null'}, signal ${signal ?? 'none'})\n`);
      close().finally(() => { process.exitCode = 1; });
    });
    child.on('error', error => {
      process.stderr.write(`${describeHostModelFailure(error)}\n`);
      close().finally(() => { process.exitCode = 1; });
    });
    process.on('SIGTERM', () => { close().finally(() => { process.exitCode = 0; }); });
  }).catch(error => {
    // A silent exit 1 here left adp-crew.service in a restart loop with an empty journal
    // (dev 2026-10-20): the authority receipt's lease had expired before the service started.
    process.stderr.write(`${describeHostModelFailure(error)}\n`);
    process.exitCode = 1;
  });
}
