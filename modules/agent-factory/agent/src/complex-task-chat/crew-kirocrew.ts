import { execFile as runFile, spawn, type ChildProcess } from 'node:child_process';
import { createHash, randomBytes } from 'node:crypto';
import { constants, createReadStream } from 'node:fs';
import { mkdir, mkdtemp, open, readFile, readlink, realpath, rename, rm, stat, symlink } from 'node:fs/promises';
import { isIPv4 } from 'node:net';
import { join } from 'node:path';
import { setTimeout as sleep } from 'node:timers/promises';
import { promisify } from 'node:util';
import { crewToolsServer, type CrewToolsServer } from './crew-tools';

export const CREW_URL = 'http://127.0.0.1:5476';
export const CREW_MODEL_PATH = '/opt/kirocrew/models/qwen3-embedding-0.6b-q8_0.gguf';
export const CREW_MODEL_SHA256 = '06507c7b42688469c4e7298b0a1e16deff06caf291cf0a5b278c308249c3e439';
const CREW_BIN = '/opt/kirocrew/venv/bin/kirocrew';
/** Kiro Crew `hooks.auto_approve_tools` globs for the ADP tools server (identity and title forms). */
export const ADP_TOOLS_AUTO_APPROVE = ['@adp-tools/*', 'mcp__adp-tools__*'];
const ACP_BIN = '/opt/kirocrew/clients/node_modules/.bin/claude-agent-acp';
const CLAUDE_BIN = '/opt/kirocrew/clients/node_modules/.bin/claude';
const DEFAULT_AGENT = '/opt/kirocrew/default-agent.json';
/** Kiro Crew's own first-run marker (`kiro_crew/kiro_prerequisite.py::_SETUP_COMPLETE_FILENAME`). */
export const KIRO_CLI_SETUP_MARKER = '.kiro_cli_setup_complete';
const execFile = promisify(runFile);

export async function consolidateKiroCrew(home: string, modelId: string,
  adapter: { baseUrl: string; secret: string }, runner: typeof execFile = execFile, signal?: AbortSignal): Promise<void> {
  if (!home.startsWith('/') || !modelId || !adapter.baseUrl || !adapter.secret) {
    throw new Error('Kiro Crew consolidation requires an admitted home and model adapter');
  }
  try {
    await runner(CREW_BIN, ['consolidate', '--all'], {
      cwd: home, timeout: 90_000, maxBuffer: 16_384, signal,
      env: { HOME: home, KIROCREW_HOME: home,
        PATH: '/opt/kirocrew/clients/node_modules/.bin:/opt/kirocrew/venv/bin:/usr/local/bin:/usr/bin:/bin',
        ANTHROPIC_BASE_URL: adapter.baseUrl, ANTHROPIC_API_KEY: adapter.secret, ANTHROPIC_MODEL: modelId,
        CLAUDE_AGENT_ACP_BIN: ACP_BIN, CLAUDE_CODE_EXECUTABLE: CLAUDE_BIN,
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1' },
    });
  } catch {
    throw new Error('Kiro Crew consolidation failed');
  }
}

async function writeConfig(path: string, value: object): Promise<void> {
  const temp = `${path}.${process.pid}.tmp`;
  const handle = await open(temp, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
  try { await handle.writeFile(`${JSON.stringify(value)}\n`); }
  finally { await handle.close(); }
  try { await rename(temp, path); }
  catch (error) { await rm(temp, { force: true }); throw error; }
}

async function readConfig(path: string, fallback: Record<string, unknown>): Promise<Record<string, unknown>> {
  let handle;
  try { handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return fallback;
    throw error;
  }
  try {
    const value: unknown = JSON.parse(await handle.readFile('utf8'));
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Kiro Crew configuration');
    return value as Record<string, unknown>;
  } finally { await handle.close(); }
}

function section(value: unknown): Record<string, unknown> {
  if (value === undefined) return {};
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Invalid Kiro Crew configuration section');
  return value as Record<string, unknown>;
}

export async function verifyEmbeddingModel(modelPath: string, expectedSha256 = CREW_MODEL_SHA256): Promise<void> {
  const model = await stat(modelPath);
  if (!model.isFile() || model.size < 1_000_000) throw new Error('Kiro Crew embedding model unavailable');
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(modelPath)) hash.update(chunk);
  if (hash.digest('hex') !== expectedSha256) throw new Error('Kiro Crew embedding model checksum mismatch');
}

export async function renderCrewConfig(home: string, modelId: string, modelPath = CREW_MODEL_PATH,
  modelSha256 = CREW_MODEL_SHA256, defaultAgent = DEFAULT_AGENT, dashboardOrigin?: string,
  shim?: ReturnType<typeof crewToolsServer>): Promise<void> {
  if (!home.startsWith('/') || !modelId || !modelPath.startsWith('/')) throw new Error('Kiro Crew requires an admitted home and model');
  if (dashboardOrigin !== undefined) {
    const origin = new URL(dashboardOrigin);
    if (origin.protocol !== 'https:' || origin.origin !== dashboardOrigin || origin.pathname !== '/' || origin.search || origin.hash) {
      throw new Error('Crew dashboard requires an HTTPS origin');
    }
  }
  await verifyEmbeddingModel(modelPath, modelSha256);
  await mkdir(home, { recursive: true, mode: 0o700 });
  const agents = join(home, '.kiro', 'agents');
  await mkdir(agents, { recursive: true, mode: 0o700 });
  if (await realpath(agents) !== join(await realpath(home), '.kiro', 'agents')) {
    throw new Error('Kiro Crew agent directory must be inside the retained home');
  }
  const configFile = join(home, 'config.json');
  const previous = await readConfig(configFile, {});
  await writeConfig(configFile, {
    ...previous,
    agent: { ...section(previous.agent), acp_backend: 'claude', member_acp_backend: 'claude', model: modelId,
      sandbox: 'auto', sandbox_allow_unsandboxed_exec: true, default_agent: 'kirocrew', session_control: true },
    memory: { ...section(previous.memory), embed_model_path: modelPath, embedding_dim: 1024 },
    dashboard: { ...section(previous.dashboard), url: dashboardOrigin ?? CREW_URL },
    // ADP tools are consented and governed by the gateway's tools policy (#8391), so Kiro Crew's own
    // approval card for them is a second prompt nobody answers on an unattended dashboard: on dev
    // 2026-10-21 a turn calling code_interpreter waited 600 s for approval and was declined (#8223
    // cause 46). Kiro Crew keys an identity-verified MCP grant on `@server/tool`; the title form covers
    // a call whose identity is not verified. Nothing else is auto-approved.
    hooks: { ...section(previous.hooks), auto_approve_tools: [...new Set([
      ...(Array.isArray(section(previous.hooks).auto_approve_tools) ? section(previous.hooks).auto_approve_tools as string[] : []),
      ...ADP_TOOLS_AUTO_APPROVE])] },
  });
  const agentFile = join(agents, 'kirocrew.json');
  const agent = await readConfig(agentFile, await readConfig(defaultAgent, {}));
  if (agent.name !== 'kirocrew' || !Array.isArray(agent.tools)) throw new Error('Invalid Kiro Crew agent specification');
  if (shim) await writeClaudeToolsServer(home, shim);
  await writeConfig(agentFile, { ...(shim ? withToolsServer(agent, shim) : agent), model: modelId });
  await seedKiroCrewFirstRun(home, agents, modelId);
  const result = JSON.parse(await readFile(configFile, 'utf8'));
  if (result.agent?.acp_backend !== 'claude' || result.agent?.member_acp_backend !== 'claude') {
    throw new Error('Kiro Crew Claude ACP backend unavailable');
  }
}

/**
 * Pass Kiro Crew's first-run gate on a home this platform provisioned (#8223).
 *
 * Kiro Crew wraps its dashboard in the Kiro CLI prerequisite gate: the application renders
 * only when `GET /api/kiro-prerequisite` reports `initial_setup_complete` or `ready`
 * (kiro_crew/kiro_prerequisite.py, `_established_installation`). `ready` comes from probing
 * `kiro-cli`, which a Crew home on this platform never has (the backend is Claude Code), and
 * `initial_setup_complete` is the marker Kiro Crew writes after a successful Kiro CLI setup, or
 * non-empty sessions/history from earlier use. A fresh platform home has neither, so every new
 * box showed "set up Kiro CLI" and no agent could start. The platform performed the setup, so
 * it records the marker exactly as Kiro Crew does (`complete\n`, owner-only) and seeds the
 * second required agent spec (`REQUIRED_KIRO_AGENT_FILES` = kirocrew.json, kirocrew-lite.json)
 * the way Kiro Crew's own `_install_lite_agent_fallback` writes it, so the gate's spec overlay
 * has nothing to repair. The lite spec pins the approved model: the loopback adapter admits
 * only that model, and Kiro Crew's `"auto"` is resolved by a Kiro account this home has none of.
 * Existing files are never rewritten; a later Kiro Crew write wins.
 */
async function seedKiroCrewFirstRun(home: string, agents: string, modelId: string): Promise<void> {
  await writeIfAbsent(join(home, KIRO_CLI_SETUP_MARKER), 'complete\n');
  await writeIfAbsent(join(agents, 'kirocrew-lite.json'),
    `${JSON.stringify({ name: 'kirocrew-lite', model: modelId, tools: [], mcpServers: {}, prompt: '' })}\n`);
}

async function writeIfAbsent(path: string, content: string): Promise<void> {
  let handle;
  try { handle = await open(path, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'EEXIST') return;
    throw error;
  }
  try { await handle.writeFile(content); }
  finally { await handle.close(); }
}

function withToolsServer(agent: Record<string, unknown>, shim: CrewToolsServer): Record<string, unknown> {
  const tools = Array.isArray(agent.tools) ? agent.tools as unknown[] : [];
  return { ...agent, mcpServers: { ...section(agent.mcpServers), 'adp-tools': shim },
    tools: [...new Set([...tools, '@adp-tools'])],
    allowedTools: [...new Set([...(Array.isArray(agent.allowedTools) ? agent.allowedTools as unknown[] : []), '@adp-tools'])] };
}

/**
 * Claude Code's first interactive start runs its onboarding (theme, then a login-method choice) unless
 * `~/.claude.json` says onboarding is complete, and it asks whether to trust a custom API key unless the
 * key's last 20 characters are in `customApiKeyResponses.approved` (its own rule). The adapter secret
 * rotates per boot, so the approval is re-seeded on every render and a stale rejection of the same key
 * is dropped. Dev 2026-10-23 (#8719): the Kiro Crew terminal and the owner shell showed the login wizard
 * although the adapter key was already in the environment.
 */
export function withClaudeOnboarding(claude: Record<string, unknown>, secret: string): Record<string, unknown> {
  const digest = secret.trim().slice(-20);
  const strings = (value: unknown) => (Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []);
  const responses = section(claude.customApiKeyResponses);
  return {
    ...claude,
    hasCompletedOnboarding: true,
    theme: typeof claude.theme === 'string' ? claude.theme : 'dark',
    customApiKeyResponses: {
      approved: [...new Set([...strings(responses.approved), digest])],
      rejected: strings(responses.rejected).filter(item => item !== digest),
    },
  };
}

async function writeClaudeToolsServer(home: string, shim: CrewToolsServer): Promise<void> {
  const claudeFile = join(home, '.claude.json');
  const claude = await readConfig(claudeFile, {});
  await writeConfig(claudeFile, withClaudeOnboarding(
    { ...claude, mcpServers: { ...section(claude.mcpServers), 'adp-tools': shim } }, shim.env.ADP_CREW_TOOLS_TOKEN));
}

/**
 * Register (or re-register) the `adp-tools` server on an already configured Crew home (#8380).
 *
 * A Dev Box renders its Crew configuration at bootstrap, before the loopback adapter exists;
 * the host model supervisor calls this once the adapter is listening so Kiro Crew and Claude
 * Code learn the current adapter address, secret and host run. Everything else in the agent
 * specification and Claude settings is preserved. Refuses a home whose agent specification is
 * missing or not the Kiro Crew one rather than inventing it.
 */
export async function renderCrewToolsServer(home: string, shim: CrewToolsServer): Promise<void> {
  const agents = join(home, '.kiro', 'agents');
  if (await realpath(agents) !== join(await realpath(home), '.kiro', 'agents')) {
    throw new Error('Kiro Crew agent directory must be inside the retained home');
  }
  const agentFile = join(agents, 'kirocrew.json');
  const agent = await readConfig(agentFile, {});
  if (agent.name !== 'kirocrew' || !Array.isArray(agent.tools)) throw new Error('Invalid Kiro Crew agent specification');
  await writeClaudeToolsServer(home, shim);
  await writeConfig(agentFile, withToolsServer(agent, shim));
}

/**
 * Keep the bundle's `gh` wrapper first for Kiro Crew's agents (#8748).
 *
 * Kiro Crew prepends `~/.local/bin` to the PATH of the agents it spawns, ahead of the bundle's
 * `host/` directory. On dev (2026-10-21) an agent had installed its own `gh` there, so after the
 * owner connected GitHub every Crew turn still saw an unauthenticated `gh` while the owner's
 * SSH shell worked. At every host start, any `~/.local/bin/gh` that is not a link to the bundle
 * wrapper is moved aside (kept, never deleted) and replaced by the link. Only that one name is
 * touched; a symlink pointing elsewhere is treated like a foreign file.
 */
export async function ensureGhWrapperFirst(home: string, wrapper = '/opt/adp/crew/current/host/gh',
  report: (line: string) => void = () => undefined): Promise<void> {
  const bin = join(home, '.local', 'bin');
  const target = join(bin, 'gh');
  await mkdir(bin, { recursive: true, mode: 0o755 });
  let current: string | undefined;
  try { current = await readlink(target); } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') { await symlink(wrapper, target); return; }
    if ((error as NodeJS.ErrnoException).code !== 'EINVAL') throw error;  // EINVAL: a regular file
  }
  if (current === wrapper) return;
  const aside = `${target}.agent-installed-${Date.now()}`;
  await rename(target, aside);
  await symlink(wrapper, target);
  report(JSON.stringify({ event: 'crew_gh_wrapper_restored', moved_aside: aside }));
}

/**
 * Forget a stale quarantine of the ADP tools server before Kiro Crew starts (#8835).
 *
 * Kiro records failed MCP probes in `<home>/mcp-quarantine.json`; after three in a row it never
 * spawns that server again until the entry is cleared. The home is the retained workspace disk, so
 * the verdict survives a Replace (dev gen 52, 2026-10-26: adp-tools stayed quarantined on a fresh
 * box). The server is this host's own shim and its listing is warmed at every start, so an old
 * verdict is history, not evidence. Only that one entry is touched; the file is otherwise kept as is.
 */
export async function clearToolsQuarantine(home: string, report: (line: string) => void = () => undefined,
  server = 'adp-tools'): Promise<void> {
  const file = join(home, 'mcp-quarantine.json');
  const state = await readConfig(file, {});
  const servers = state.servers;
  if (!servers || typeof servers !== 'object' || Array.isArray(servers) || !(server in servers)) return;
  const { [server]: entry, ...rest } = servers as Record<string, unknown>;
  await writeConfig(file, { ...state, servers: rest });
  const record = entry && typeof entry === 'object' ? entry as Record<string, unknown> : {};
  report(JSON.stringify({ event: 'crew_tools_quarantine_cleared', server, fails: record.fails,
    crossed_at: record.crossed_at, last_error: record.last_error }));
}

export async function startKiroCrew(
  home: string, modelId: string, signal?: AbortSignal,
  options: { modelPath?: string; modelSha256?: string; defaultAgent?: string; toolsRunId?: string;
    adapter?: { baseUrl: string; secret: string }; podIP?: string; dashboardOrigin?: string; authScratch?: string;
    checkProfile?: (env: NodeJS.ProcessEnv, key: string) => Promise<string>;
    launch?: (env: NodeJS.ProcessEnv) => ChildProcess;
    request?: typeof fetch; now?: () => number; pause?: (ms: number) => Promise<void> } = {},
): Promise<ChildProcess> {
  if (!options.adapter || !/^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/.test(options.adapter.baseUrl) ||
    !/^[a-f0-9]{64}$/.test(options.adapter.secret)) throw new Error('Scoped Crew model adapter unavailable');
  if (options.podIP && (!isIPv4(options.podIP) ||
      !/^(10\.|192\.168\.|172\.(1[6-9]|2[0-9]|3[01])\.)/.test(options.podIP))) {
    throw new Error('Crew dashboard requires a private pod IP');
  }
  await renderCrewConfig(home, modelId, options.modelPath, options.modelSha256, options.defaultAgent,
    options.dashboardOrigin, options.toolsRunId ? crewToolsServer(options.adapter, options.toolsRunId) : undefined);
  signal?.throwIfAborted();
  if (options.dashboardOrigin) {
    const scratch = await mkdtemp(join(options.authScratch ?? '/tmp', 'crew-auth-'));
    const secret = join(scratch, 'token_signing.key');
    const handle = await open(secret, constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | constants.O_NOFOLLOW, 0o600);
    try { await handle.writeFile(randomBytes(32)); }
    finally { await handle.close(); }
    const retainedKey = join(home, 'token_signing.key');
    await rm(retainedKey, { force: true });
    await symlink(secret, retainedKey);
  }
  const env: NodeJS.ProcessEnv = { HOME: home, KIROCREW_HOME: home, PATH: '/opt/kirocrew/clients/node_modules/.bin:/opt/kirocrew/venv/bin:/usr/local/bin:/usr/bin:/bin',
    CLAUDE_AGENT_ACP_BIN: ACP_BIN, CLAUDE_CODE_EXECUTABLE: CLAUDE_BIN,
    ANTHROPIC_BASE_URL: options.adapter.baseUrl, ANTHROPIC_API_KEY: options.adapter.secret, ANTHROPIC_MODEL: modelId,
    ...(options.podIP ? { KIROCREW_BIND: '0.0.0.0' } : {}),
    ...(options.dashboardOrigin ? { KIROCREW_CORS_ORIGINS: options.dashboardOrigin } : {}),
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: '1', DISABLE_TELEMETRY: '1' };
  const checkProfile = options.checkProfile ?? (async (environment: NodeJS.ProcessEnv, key: string) =>
    (await execFile(CREW_BIN, ['config', 'get', key], { cwd: home, env: environment, timeout: 3_000 })).stdout.trim());
  for (const [key, expected] of [['agent.acp_backend', 'claude'], ['agent.member_acp_backend', 'claude'],
    ['memory.embed_model_path', options.modelPath ?? CREW_MODEL_PATH]]) {
    if (await checkProfile(env, key) !== expected) throw new Error('Kiro Crew native profile incompatible: ' + key);
  }
  const child = options.launch?.(env) ?? spawn(CREW_BIN, ['gateway', '--no-open', '--no-tunnel', '--no-crons', '--port', '5476'],
    { cwd: home, env, stdio: 'ignore' });
  const request = options.request ?? fetch;
  const now = options.now ?? (() => performance.now());
  const pause = options.pause ?? ((ms: number) => sleep(ms).then(() => undefined));
  const deadline = now() + 30_000;
  let startupError: Error | undefined;
  child.on('error', (error: Error) => { startupError = error; });
  try {
    while (now() < deadline) {
      signal?.throwIfAborted();
      if (startupError) throw startupError;
      if (child.exitCode !== null || child.signalCode !== null) throw new Error('Kiro Crew gateway exited before readiness');
      try {
        const health = await request(`${CREW_URL}/api/health`, { signal: AbortSignal.timeout(1_000) });
        const status = (await health.json()) as { ok?: boolean; app?: string };
        if (health.ok && status.ok === true && status.app === 'kirocrew') {
          const config = JSON.parse(await readFile(join(home, 'config.json'), 'utf8'));
          if (config.agent?.acp_backend !== 'claude' || config.agent?.member_acp_backend !== 'claude') {
            throw new Error('Kiro Crew Claude ACP backend unavailable');
          }
          return child;
        }
      } catch (error) {
        if (error instanceof Error && error.message === 'Kiro Crew Claude ACP backend unavailable') throw error;
      }
      await pause(Math.min(250, Math.max(0, deadline - now())));
    }
    throw new Error('Kiro Crew gateway health timed out after 30 seconds');
  } catch (error) {
    child.kill('SIGTERM');
    throw error;
  }
}
