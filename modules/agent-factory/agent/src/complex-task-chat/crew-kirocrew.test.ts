import { mkdtempSync, mkdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync, readlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn, type ChildProcess } from 'node:child_process';
import { ADP_TOOLS_AUTO_APPROVE, CREW_MODEL_SHA256, KIRO_CLI_SETUP_MARKER, clearToolsQuarantine, consolidateKiroCrew, ensureGhWrapperFirst, renderCrewConfig, renderCrewToolsServer, startKiroCrew, withClaudeOnboarding } from './crew-kirocrew';
import { crewHostToolsServer, crewToolsServer } from './crew-tools';
import { crewHostModelClient } from './crew-host-model-client';
import { startCrewModelAdapter } from './crew-model-adapter';
import { ChatDataClient } from './gateway/chat-data-client';

let root: string;
let home: string;
let model: string;
let modelSha256: string;
let defaults: string;
const adapter = { baseUrl: 'http://127.0.0.1:4555', secret: 'a'.repeat(64) };
beforeEach(() => {
  root = mkdtempSync(join(tmpdir(), 'adp-crew-'));
  home = join(root, 'workspace');
  model = join(root, 'model.gguf');
  const fixture = Buffer.alloc(1_000_000, 0x51);
  modelSha256 = createHash('sha256').update(fixture).digest('hex');
  writeFileSync(model, fixture);
  defaults = join(root, 'default-agent.json');
  writeFileSync(defaults, JSON.stringify({ name: 'kirocrew', model: 'auto', tools: ['fs_read', '@kirocrew-core'], allowedTools: [] }));
});
afterEach(() => rmSync(root, { recursive: true, force: true }));

function nativeProfile(_env: NodeJS.ProcessEnv, key: string): Promise<string> {
  return Promise.resolve(({ 'agent.acp_backend': 'claude', 'agent.member_acp_backend': 'claude',
    'memory.embed_model_path': model } as Record<string, string>)[key] ?? '');
}

function childProcess() {
  const kill = jest.fn(() => true);
  return { exitCode: null, signalCode: null, kill, on: jest.fn() } as unknown as ChildProcess;
}

test('native stop consolidation processes ten pending messages through the admitted adapter', async () => {
  const messages = Array.from({ length: 10 }, (_, index) => `pending-${index}`);
  const runner = jest.fn(async (_executable: string, _args: string[], _options: object) => {
    messages.splice(0);
    return { stdout: '', stderr: '' };
  });
  await consolidateKiroCrew(home, 'approved-model', adapter, runner as unknown as Parameters<typeof consolidateKiroCrew>[3]);
  expect(messages).toHaveLength(0);
  expect(runner).toHaveBeenCalledWith('/opt/kirocrew/venv/bin/kirocrew', ['consolidate', '--all'],
    expect.objectContaining({ cwd: home, timeout: 90_000,
      env: expect.objectContaining({ HOME: home, KIROCREW_HOME: home, ANTHROPIC_BASE_URL: adapter.baseUrl,
        ANTHROPIC_API_KEY: adapter.secret, ANTHROPIC_MODEL: 'approved-model' }) }));
  await expect(consolidateKiroCrew(home, 'approved-model', adapter, (async () => {
    throw new Error('private message must not be logged');
  }) as never)).rejects.toThrow('Kiro Crew consolidation failed');
});

test('renders a native Claude ACP profile in the retained home and preserves unrelated settings', async () => {
  mkdirSync(home);
  writeFileSync(join(home, 'config.json'), JSON.stringify({ tunnel: { enabled: false }, agent: { streaming: false } }));
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  const config = JSON.parse(readFileSync(join(home, 'config.json'), 'utf8'));
  expect(config.tunnel).toEqual({ enabled: false });
  expect(config.agent).toEqual({ streaming: false, acp_backend: 'claude', member_acp_backend: 'claude',
    model: 'approved-model', sandbox: 'auto', sandbox_allow_unsandboxed_exec: true, default_agent: 'kirocrew', session_control: true });
  expect(config.memory).toEqual({ embed_model_path: model, embedding_dim: 1024 });
  expect(config.dashboard.url).toBe('http://127.0.0.1:5476');
  expect(JSON.parse(readFileSync(join(home, '.kiro/agents/kirocrew.json'), 'utf8'))).toEqual({
    name: 'kirocrew', tools: ['fs_read', '@kirocrew-core'], allowedTools: [], model: 'approved-model',
  });
});

test('a fresh home passes Kiro Crew\'s first-run gate: setup marker and lite spec are seeded owner-only', async () => {
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  const marker = join(home, KIRO_CLI_SETUP_MARKER);
  expect(readFileSync(marker, 'utf8')).toBe('complete\n');
  expect(statSync(marker).mode & 0o777).toBe(0o600);
  const lite = join(home, '.kiro/agents/kirocrew-lite.json');
  expect(JSON.parse(readFileSync(lite, 'utf8'))).toEqual({ name: 'kirocrew-lite', model: 'approved-model', tools: [], mcpServers: {}, prompt: '' });
  expect(statSync(lite).mode & 0o777).toBe(0o600);
  // The Claude backend configuration is untouched by the seeding.
  const config = JSON.parse(readFileSync(join(home, 'config.json'), 'utf8'));
  expect(config.agent.acp_backend).toBe('claude');
  expect(JSON.parse(readFileSync(join(home, '.kiro/agents/kirocrew.json'), 'utf8')).name).toBe('kirocrew');
});

test('an existing marker or lite spec is never rewritten by a re-render', async () => {
  mkdirSync(join(home, '.kiro', 'agents'), { recursive: true });
  writeFileSync(join(home, KIRO_CLI_SETUP_MARKER), 'complete\n', { mode: 0o644 });
  writeFileSync(join(home, '.kiro/agents/kirocrew-lite.json'), JSON.stringify({ name: 'kirocrew-lite', model: 'auto', tools: ['fs_read'] }));
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  expect(statSync(join(home, KIRO_CLI_SETUP_MARKER)).mode & 0o777).toBe(0o644);
  expect(JSON.parse(readFileSync(join(home, '.kiro/agents/kirocrew-lite.json'), 'utf8'))).toEqual({ name: 'kirocrew-lite', model: 'auto', tools: ['fs_read'] });
});

test('healthy response only admits a rendered Claude backend', async () => {
  const child = childProcess();
  const launch = jest.fn((_env: NodeJS.ProcessEnv) => child);
  const request = jest.fn(async () => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })));
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256, adapter, defaultAgent: defaults, checkProfile: nativeProfile, launch, request })).resolves.toBe(child);
  expect(launch.mock.calls).toHaveLength(1);
  expect(launch.mock.calls[0][0]).toMatchObject({ HOME: home, KIROCREW_HOME: home,
    ANTHROPIC_BASE_URL: adapter.baseUrl, ANTHROPIC_API_KEY: adapter.secret, ANTHROPIC_MODEL: 'approved-model' });
  expect(launch.mock.calls[0][0].AWS_ACCESS_KEY_ID).toBeUndefined();
  expect(request.mock.calls).toHaveLength(1);
  expect(child.kill).not.toHaveBeenCalled();
});

test('a healthy HTTP response with another ACP backend still fails startup', async () => {
  const child = childProcess();
  const request = jest.fn(async () => {
    const path = join(home, 'config.json');
    const config = JSON.parse(readFileSync(path, 'utf8'));
    config.agent.acp_backend = 'kiro-cli';
    writeFileSync(path, JSON.stringify(config));
    return new Response(JSON.stringify({ ok: true, app: 'kirocrew' }));
  });
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256, adapter, defaultAgent: defaults, checkProfile: nativeProfile, launch: () => child, request }))
    .rejects.toThrow('Claude ACP backend unavailable');
  expect(child.kill).toHaveBeenCalledWith('SIGTERM');
});

test('a symlinked agent directory cannot escape the retained home', async () => {
  mkdirSync(join(home, '.kiro'), { recursive: true });
  symlinkSync(root, join(home, '.kiro', 'agents'));
  await expect(renderCrewConfig(home, 'approved-model', model, modelSha256, defaults)).rejects.toThrow('inside the retained home');
});

test('readiness times out in 30 seconds, stopping the gateway', async () => {
  const child = childProcess();
  let elapsed = 0;
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256, adapter, defaultAgent: defaults, checkProfile: nativeProfile, launch: () => child,
    request: async () => new Response(JSON.stringify({ ok: false })), now: () => elapsed,
    pause: async ms => { elapsed += ms; } })).rejects.toThrow('timed out after 30 seconds');
  expect(child.kill).toHaveBeenCalledWith('SIGTERM');
});

test('a missing or non-loopback model adapter fails before Crew starts', async () => {
  const launch = jest.fn();
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
    defaultAgent: defaults, checkProfile: nativeProfile, launch })).rejects.toThrow('Scoped Crew model adapter unavailable');
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
    defaultAgent: defaults, checkProfile: nativeProfile, launch,
    adapter: { ...adapter, baseUrl: 'https://provider.example.test' } })).rejects.toThrow('Scoped Crew model adapter unavailable');
  expect(launch).not.toHaveBeenCalled();
});

test('a native profile that resolves to kiro-cli fails before gateway launch', async () => {
  const launch = jest.fn();
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256, adapter,
    defaultAgent: defaults, launch, checkProfile: async (env, key) => key === 'agent.acp_backend'
      ? 'kiro-cli' : nativeProfile(env, key) })).rejects.toThrow('native profile incompatible: agent.acp_backend');
  expect(launch).not.toHaveBeenCalled();
});

test('a corrupt baked embedding model is rejected before launch, with no download request', async () => {
  const launch = jest.fn();
  const request = jest.fn();
  writeFileSync(model, Buffer.alloc(1_000_000, 0x52));
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256, adapter, defaultAgent: defaults, checkProfile: nativeProfile, launch, request }))
    .rejects.toThrow('checksum mismatch');
  expect(launch).not.toHaveBeenCalled();
  expect(request).not.toHaveBeenCalled();
});

test('the image pins the stable unmodified wheel, native defaults and locked ACP clients', () => {
  const dockerfile = readFileSync(join(__dirname, '..', '..', 'Dockerfile.sandbox'), 'utf8');
  const clients = JSON.parse(readFileSync(join(__dirname, '..', '..', 'kirocrew-clients', 'package-lock.json'), 'utf8'));
  expect(dockerfile).toContain('76f280e1716e0eb9b4b6b590d60f3cee4bed364b19ac636af1ceee5dd2fbe750');
  expect(dockerfile).toContain('https://download.crew.kiro.dev/cli/stable/0.11.2/kirocrew-0.11.2-py3-none-any.whl');
  expect(dockerfile).toContain('python:3.12-slim-trixie');
  expect(dockerfile).toContain('defaults.json');
  expect(dockerfile).toContain('COPY --from=wheel /opt/kirocrew/default-agent.json /opt/kirocrew/default-agent.json');
  for (const [name, version] of [['@agentclientprotocol/claude-agent-acp', '0.100.0'],
    ['@anthropic-ai/claude-code', '2.1.318']]) {
    expect(clients.packages['node_modules/' + name].version).toBe(version);
    expect(clients.packages['node_modules/' + name].integrity).toMatch(/^sha512-/);
  }
});

test('the image bake pins the upstream model hash and never references a runtime download', () => {
  const dockerfile = readFileSync(join(__dirname, '..', '..', 'Dockerfile.sandbox'), 'utf8');
  expect(dockerfile).toContain(CREW_MODEL_SHA256);
  expect(dockerfile).toContain('sha256sum -c -');
  expect(dockerfile).toContain('COPY --from=embedding /opt/kirocrew/models/ /opt/kirocrew/models/');
  expect(dockerfile).toContain('https://d3j0sthz5doyui.cloudfront.net/models/qwen3-embedding-0.6b.gguf');
});

test('a missing embedding model cannot start the gateway or download one', async () => {
  const launch = jest.fn();
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: join(root, 'missing.gguf'), adapter, launch }))
    .rejects.toThrow();
  expect(launch).not.toHaveBeenCalled();
});

test.each(['10.3.4.5', '172.16.1.2', '172.31.1.2', '192.168.1.2'])(
  'exposes Kiro Crew for private pod IP %s and renders the externally routed origin', async podIP => {
  const child = childProcess();
  const launch = jest.fn((_env: NodeJS.ProcessEnv) => child);
  const request = jest.fn(async (_input: string | URL | Request) => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })));
  await startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
    defaultAgent: defaults, adapter, checkProfile: nativeProfile, launch, request,
    podIP, dashboardOrigin: 'https://crew.example.test', authScratch: root });
  expect(JSON.parse(readFileSync(join(home, 'config.json'), 'utf8')).dashboard.url).toBe('https://crew.example.test');
  expect(readlinkSync(join(home, 'token_signing.key'))).toMatch(/^\/.*crew-auth-.*\/token_signing\.key$/);
  expect(launch.mock.calls[0][0]).toMatchObject({ KIROCREW_BIND: '0.0.0.0',
    KIROCREW_CORS_ORIGINS: 'https://crew.example.test' });
  expect(request.mock.calls[0][0]).toBe('http://127.0.0.1:5476/api/health');
});

test.each(['10.256.1.2', '192.168.1.2.3', '172.16.1.2.3', '172.15.1.2', '172.32.1.2',
  '192.169.1.2', '127.0.0.1', '8.8.8.8'])(
  'rejects invalid or non-private pod IP %s before launching Kiro Crew', async podIP => {
  const launch = jest.fn(() => childProcess());
  const request = jest.fn(async () => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })));
  await expect(startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
    defaultAgent: defaults, adapter, checkProfile: nativeProfile, launch, request,
    podIP, dashboardOrigin: 'https://crew.example.test', authScratch: root }))
    .rejects.toThrow('Crew dashboard requires a private pod IP');
  expect(launch).not.toHaveBeenCalled();
  expect(request).not.toHaveBeenCalled();
});

test('rejects an insecure dashboard origin before launching', async () => {
  await expect(renderCrewConfig(home, 'approved-model', model, modelSha256, defaults,
    'http://crew.example.test')).rejects.toThrow('HTTPS origin');
});

test('a replacement pod rotates its signing key without losing its retained profile', async () => {
  const options = { modelPath: model, modelSha256, defaultAgent: defaults, adapter,
    podIP: '10.3.4.5', dashboardOrigin: 'https://crew.example.test', authScratch: root,
    checkProfile: nativeProfile, launch: () => childProcess(),
    request: async () => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })) };
  await startKiroCrew(home, 'approved-model', undefined, options);
  const firstKey = readFileSync(join(home, 'token_signing.key'));
  const firstPath = readlinkSync(join(home, 'token_signing.key'));
  await startKiroCrew(home, 'approved-model', undefined, options);
  expect(readlinkSync(join(home, 'token_signing.key'))).not.toBe(firstPath);
  expect(readFileSync(join(home, 'token_signing.key'))).not.toEqual(firstKey);
  expect(JSON.parse(readFileSync(join(home, 'config.json'), 'utf8')).agent.model).toBe('approved-model');
});

test('registers the bundled ADP shim for Claude and Crew while preserving existing servers', async () => {
  mkdirSync(home);
  writeFileSync(join(home, '.claude.json'), JSON.stringify({ theme: 'dark', mcpServers: { other: { command: 'other' } } }));
  mkdirSync(join(home, '.kiro', 'agents'), { recursive: true });
  writeFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), JSON.stringify({
    name: 'kirocrew', tools: ['@kirocrew-core'], mcpServers: { other: { command: 'other' } },
  }));
  const shim = crewToolsServer(adapter, '00000000-0000-4000-8000-000000000001');
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults, undefined, shim);
  const claude = JSON.parse(readFileSync(join(home, '.claude.json'), 'utf8'));
  const crew = JSON.parse(readFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), 'utf8'));
  // #8719: interactive `claude` must open at the prompt — onboarding complete, the adapter key approved.
  expect(claude).toEqual({ theme: 'dark', hasCompletedOnboarding: true,
    customApiKeyResponses: { approved: [shim.env.ADP_CREW_TOOLS_TOKEN.slice(-20)], rejected: [] },
    mcpServers: { other: { command: 'other' }, 'adp-tools': shim } });
  expect(crew.mcpServers).toEqual({ other: { command: 'other' }, 'adp-tools': shim });
  expect(crew.tools).toEqual(['@kirocrew-core', '@adp-tools']);
  expect(crew.allowedTools).toEqual(['@adp-tools']);
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults, undefined, shim);
  expect(JSON.parse(readFileSync(join(home, '.claude.json'), 'utf8'))).toEqual(claude);
  const dockerfile = readFileSync(join(__dirname, '..', '..', 'Dockerfile.sandbox'), 'utf8');
  expect(dockerfile).toContain('COPY --from=adp-cli adp-mcp.py /opt/adp/adp-mcp.py');
  expect(dockerfile).toContain('COPY --from=adp-cli adp_common.py /opt/adp/adp_common.py');
  expect(dockerfile).toContain('ln -s /opt/adp/adp /usr/local/bin/adp');
});

test('headless private Crew binds to its pod IP without enabling browser origin', async () => {
  const launch = jest.fn((_env: NodeJS.ProcessEnv) => childProcess());
  const request = jest.fn(async () => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })));
  await startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
    defaultAgent: defaults, adapter, checkProfile: nativeProfile, launch, request, podIP: '10.3.4.5' });
  expect(launch.mock.calls[0][0]).toMatchObject({ KIROCREW_BIND: '0.0.0.0' });
  expect(launch.mock.calls[0][0].KIROCREW_CORS_ORIGINS).toBeUndefined();
});

test('production Crew configuration runs the bundled shim through its scoped gateway client', async () => {
  const runId = '00000000-0000-4000-8000-000000000001';
  const forwarded: Array<Record<string, unknown>> = [];
  const gateway = jest.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
    if (String(url).endsWith('/bootstrap')) return new Response(JSON.stringify({ capability: 'synthetic.capability',
      run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent', attempt: 1, lease_generation: 1,
      expires_at: Math.floor(Date.now() / 1000) + 300 }), { headers: { 'content-type': 'application/json' } });
    expect(String(url)).toBe('https://gateway.example.test/v1/chat/data/tools');
    expect(options?.headers).toMatchObject({ Authorization: 'Bearer synthetic.capability',
      'X-Adp-Workload-Token': 'synthetic.workload', 'x-agent-runid': runId });
    const request = JSON.parse(options?.body as string);
    expect(request).toMatchObject({ run_id: 'run-a', session_id: 'session-a' });
    forwarded.push(request);
    const body = request.path === '/tools' ? { tools: [{ name: 'websearch___search', description: 'Search', inputSchema: { type: 'object' } }] } :
      { request_id: request.body.request_id, execution_id: '00000000-0000-4000-8000-000000000002',
        status: 'confirmed', result_status: 'success', receipt_id: 'receipt-one', root_human_id: 'human-one', result: { text: 'found' } };
    return new Response(JSON.stringify({ status: 200, body }), { headers: { 'content-type': 'application/json' } });
  });
  const local = await startCrewModelAdapter(new ChatDataClient({ baseUrl: 'https://gateway.example.test',
    workloadToken: async () => 'synthetic.workload', crewRunId: runId }), 'approved-model');
  try {
    await startKiroCrew(home, 'approved-model', undefined, { modelPath: model, modelSha256,
      defaultAgent: defaults, adapter: local, toolsRunId: runId, checkProfile: nativeProfile,
      launch: () => childProcess(), request: async () => new Response(JSON.stringify({ ok: true, app: 'kirocrew' })) });
    const config = JSON.parse(readFileSync(join(home, '.claude.json'), 'utf8')).mcpServers['adp-tools'];
    const cli = join(__dirname, '..', '..', '..', '..', 'gateway', 'cli', 'adp');
    const messages = [{ jsonrpc: '2.0', id: 1, method: 'tools/list' }, { jsonrpc: '2.0', id: 2, method: 'tools/call',
      params: { name: 'websearch___search', arguments: { query: 'example' } } }];
    const replies = await new Promise<Array<any>>((resolve, reject) => {
      const shim = spawn('bash', [cli, ...config.args], { env: { PATH: process.env.PATH, HOME: home, ...config.env } });
      let output = '';
      let errors = '';
      const timeout = setTimeout(() => { shim.kill('SIGKILL'); reject(new Error('shim timed out')); }, 10_000);
      shim.stdout.on('data', chunk => { output += chunk; });
      shim.stderr.on('data', chunk => { errors += chunk; });
      shim.on('error', reject);
      shim.on('exit', code => {
        clearTimeout(timeout);
        if (code !== 0) reject(new Error(`shim exited ${code}: ${errors}`));
        else resolve(output.trim().split('\n').map(line => JSON.parse(line)));
      });
      shim.stdin.end(messages.map(message => JSON.stringify(message)).join('\n') + '\n');
    });
    expect(replies[0].result.tools).toEqual(expect.arrayContaining([expect.objectContaining({ name: 'websearch___search' })]));
    expect(replies[1].result.isError).toBe(false);
    expect(JSON.parse(replies[1].result.content[0].text)).toMatchObject({ status: 'confirmed', root_human_id: 'human-one' });
    expect(forwarded.map(request => request.path)).toEqual(['/tools', '/tools/call']);
    local.suspend();
    await expect(localFetch()).resolves.toBe(400);
    expect(forwarded).toHaveLength(2);
  } finally {
    gateway.mockRestore();
    await local.close();
  }
  async function localFetch() {
    const { get } = await import('node:http');
    return new Promise<number>((resolve, reject) => {
      const request = get(`${local.baseUrl}/tools`, { headers: { Authorization: `Bearer ${local.secret}` } }, response => {
        response.resume(); response.on('end', () => resolve(response.statusCode!));
      });
      request.on('error', reject);
    });
  }
}, 20_000);

test('a Dev Box registers the bundled shim on its configured home without touching the rest (#8380)', async () => {
  mkdirSync(join(home, '.kiro', 'agents'), { recursive: true });
  writeFileSync(join(home, 'config.json'), JSON.stringify({ agent: { model: 'approved-model' } }));
  writeFileSync(join(home, '.claude.json'), JSON.stringify({ theme: 'dark', mcpServers: { other: { command: 'other' } } }));
  writeFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), JSON.stringify({
    name: 'kirocrew', model: 'approved-model', tools: ['@kirocrew-core'], mcpServers: { other: { command: 'other' } },
  }));
  const runId = '00000000-0000-4000-8000-000000000001';
  const shim = crewHostToolsServer(adapter, runId);
  expect(shim).toEqual({ command: '/opt/adp/crew/current/venv/bin/python3', args: ['/opt/adp/crew/current/host/adp/adp-mcp.py'],
    env: { ADP_CREW_TOOLS_URL: adapter.baseUrl, ADP_CREW_TOOLS_TOKEN: adapter.secret, ADP_CREW_RUN_ID: runId } });
  await renderCrewToolsServer(home, shim);
  const claude = JSON.parse(readFileSync(join(home, '.claude.json'), 'utf8'));
  const crew = JSON.parse(readFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), 'utf8'));
  // #8719: interactive `claude` must open at the prompt — onboarding complete, the adapter key approved.
  expect(claude).toEqual({ theme: 'dark', hasCompletedOnboarding: true,
    customApiKeyResponses: { approved: [shim.env.ADP_CREW_TOOLS_TOKEN.slice(-20)], rejected: [] },
    mcpServers: { other: { command: 'other' }, 'adp-tools': shim } });
  expect(crew).toEqual({ name: 'kirocrew', model: 'approved-model', tools: ['@kirocrew-core', '@adp-tools'],
    mcpServers: { other: { command: 'other' }, 'adp-tools': shim }, allowedTools: ['@adp-tools'] });
  expect(JSON.parse(readFileSync(join(home, 'config.json'), 'utf8'))).toEqual({ agent: { model: 'approved-model' } });
  // A new boot re-registers with the new adapter binding; nothing accumulates.
  const rotated = crewHostToolsServer({ baseUrl: 'http://127.0.0.1:4777', secret: 'b'.repeat(64) }, runId);
  await renderCrewToolsServer(home, rotated);
  expect(JSON.parse(readFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), 'utf8')).mcpServers).toEqual({ other: { command: 'other' }, 'adp-tools': rotated });
  expect(JSON.parse(readFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), 'utf8')).tools).toEqual(['@kirocrew-core', '@adp-tools']);
  expect(() => crewHostToolsServer(adapter, runId, '/opt/../etc')).toThrow('unavailable');
  expect(() => crewHostToolsServer(adapter, undefined)).toThrow('unavailable');
});

test('a Dev Box refuses to invent a Crew agent specification for the shim (#8380)', async () => {
  mkdirSync(join(home, '.kiro', 'agents'), { recursive: true });
  const shim = crewHostToolsServer(adapter, '00000000-0000-4000-8000-000000000001');
  await expect(renderCrewToolsServer(home, shim)).rejects.toThrow('Invalid Kiro Crew agent specification');
  writeFileSync(join(home, '.kiro', 'agents', 'kirocrew.json'), JSON.stringify({ name: 'other', tools: [] }));
  await expect(renderCrewToolsServer(home, shim)).rejects.toThrow('Invalid Kiro Crew agent specification');
  expect(() => readFileSync(join(home, '.claude.json'))).toThrow();
});

test('the bundled shim on a Dev Box lists and calls ADP tools through the host adapter (#8380)', async () => {
  const runId = '00000000-0000-4000-8000-000000000001';
  const relayed: Array<Record<string, unknown>> = [];
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit) => {
    if (String(url).endsWith('/model-token/refresh')) return new Response(null, { status: 204 });
    expect(String(url)).toBe('https://exampleapi.execute-api.us-east-1.amazonaws.com/dev/agent/crew/tools');
    expect(init?.headers).toMatchObject({ 'x-agent-runid': runId, 'x-adp-crew-model-token': 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
    const request = JSON.parse(String(init?.body));
    relayed.push(request);
    const body = request.path === '/tools' ? { tools: [{ name: 'websearch___search', description: 'Search', inputSchema: { type: 'object' } }] } :
      { request_id: request.body.request_id, execution_id: '00000000-0000-4000-8000-000000000002',
        status: 'confirmed', result_status: 'success', receipt_id: 'receipt-one', root_human_id: 'human-one', result: { text: 'found' } };
    return new Response(JSON.stringify({ status: 200, body }), { status: 200 });
  });
  const local = await startCrewModelAdapter(crewHostModelClient({
    gatewayUrl: 'https://exampleapi.execute-api.us-east-1.amazonaws.com/dev', region: 'us-east-1', modelId: 'approved-model', runId,
    currentRun: () => runId, send, modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature',
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic-key', sessionToken: 'synthetic-session' }),
  }), 'approved-model');
  try {
    const shim = crewHostToolsServer(local, runId);
    const script = join(__dirname, '..', '..', '..', '..', 'gateway', 'cli', 'adp-mcp.py');
    const messages = [{ jsonrpc: '2.0', id: 1, method: 'tools/list' }, { jsonrpc: '2.0', id: 2, method: 'tools/call',
      params: { name: 'websearch___search', arguments: { query: 'example' } } }];
    const replies = await new Promise<Array<any>>((resolve, reject) => {
      const child = spawn('python3', [script], { env: { PATH: process.env.PATH, HOME: home, ...shim.env } });
      let output = '';
      let errors = '';
      const timeout = setTimeout(() => { child.kill('SIGKILL'); reject(new Error('shim timed out')); }, 10_000);
      child.stdout.on('data', chunk => { output += chunk; });
      child.stderr.on('data', chunk => { errors += chunk; });
      child.on('error', reject);
      child.on('exit', code => {
        clearTimeout(timeout);
        if (code !== 0) reject(new Error(`shim exited ${code}: ${errors}`));
        else resolve(output.trim().split('\n').map(line => JSON.parse(line)));
      });
      child.stdin.end(messages.map(message => JSON.stringify(message)).join('\n') + '\n');
    });
    expect(replies[0].result.tools.map((tool: { name: string }) => tool.name)).toContain('websearch___search');
    expect(replies[1].error).toBeUndefined();
    expect(replies[1].result.isError).toBe(false);
    expect(JSON.parse(replies[1].result.content[0].text)).toMatchObject({ status: 'confirmed', root_human_id: 'human-one' });
    expect(relayed.map(request => request.path)).toEqual(['/tools', '/tools/call']);
    expect(relayed[1].body).toMatchObject({ tool: 'websearch___search', arguments: { query: 'example' } });
  } finally { await local.close(); }
});


test('auto-approves only the ADP tools server in Kiro Crew and keeps the owner\'s own globs (#8223 cause 46)', async () => {
  mkdirSync(home);
  writeFileSync(join(home, 'config.json'), JSON.stringify({ hooks: { auto_approve_tools: ['@kirocrew-computer/*'], other: 1 } }));
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  const config = JSON.parse(readFileSync(join(home, 'config.json'), 'utf8'));
  expect(config.hooks).toEqual({ other: 1, auto_approve_tools: ['@kirocrew-computer/*', ...ADP_TOOLS_AUTO_APPROVE] });
  expect(ADP_TOOLS_AUTO_APPROVE).toEqual(['@adp-tools/*', 'mcp__adp-tools__*']);
  // Rendering again is idempotent.
  await renderCrewConfig(home, 'approved-model', model, modelSha256, defaults);
  expect(JSON.parse(readFileSync(join(home, 'config.json'), 'utf8')).hooks.auto_approve_tools)
    .toEqual(['@kirocrew-computer/*', ...ADP_TOOLS_AUTO_APPROVE]);
});


test('a rejected adapter key is approved again on the next render and the owner theme is kept (#8719)', () => {
  const secret = 'k'.repeat(64);
  const seeded = withClaudeOnboarding({ theme: 'light', customApiKeyResponses: { approved: ['old-key-digest-xxxxx'], rejected: [secret.slice(-20)] },
    mcpServers: { 'adp-tools': { command: 'x' } } }, secret);
  expect(seeded).toEqual({ theme: 'light', hasCompletedOnboarding: true,
    customApiKeyResponses: { approved: ['old-key-digest-xxxxx', secret.slice(-20)], rejected: [] }, mcpServers: { 'adp-tools': { command: 'x' } } });
});


describe("the bundle's gh wrapper stays first on Kiro Crew's PATH (#8748)", () => {
  const wrapper = '/opt/adp/crew/current/host/gh';

  it('links ~/.local/bin/gh to the wrapper when absent and leaves a correct link alone', async () => {
    const home = mkdtempSync(join(tmpdir(), 'crew-gh-'));
    try {
      const reported: string[] = [];
      await ensureGhWrapperFirst(home, wrapper, line => reported.push(line));
      expect(readlinkSync(join(home, '.local', 'bin', 'gh'))).toBe(wrapper);
      await ensureGhWrapperFirst(home, wrapper, line => reported.push(line));
      expect(readlinkSync(join(home, '.local', 'bin', 'gh'))).toBe(wrapper);
      expect(reported).toEqual([]);
    } finally { rmSync(home, { recursive: true, force: true }); }
  });

  it('moves an agent-installed gh binary or a foreign link aside, keeps it, and reports once', async () => {
    // Dev 2026-10-21: a Crew agent downloaded gh 2.103.0 into ~/.local/bin, which Kiro Crew puts ahead of
    // the bundle on PATH; every Crew turn then saw an unauthenticated gh after the owner connected GitHub.
    const home = mkdtempSync(join(tmpdir(), 'crew-gh-'));
    try {
      mkdirSync(join(home, '.local', 'bin'), { recursive: true });
      writeFileSync(join(home, '.local', 'bin', 'gh'), '#!/bin/sh\necho stale gh\n', { mode: 0o755 });
      const reported: string[] = [];
      await ensureGhWrapperFirst(home, wrapper, line => reported.push(line));
      expect(readlinkSync(join(home, '.local', 'bin', 'gh'))).toBe(wrapper);
      const moved = JSON.parse(reported[0]) as { event: string; moved_aside: string };
      expect(moved.event).toBe('crew_gh_wrapper_restored');
      expect(readFileSync(moved.moved_aside, 'utf8')).toContain('stale gh');
      rmSync(join(home, '.local', 'bin', 'gh'));
      symlinkSync('/usr/bin/gh', join(home, '.local', 'bin', 'gh'));
      await ensureGhWrapperFirst(home, wrapper, line => reported.push(line));
      expect(readlinkSync(join(home, '.local', 'bin', 'gh'))).toBe(wrapper);
      expect(reported).toHaveLength(2);
    } finally { rmSync(home, { recursive: true, force: true }); }
  });
});


describe('stale adp-tools quarantine on the retained home (#8835)', () => {
  const file = () => join(home, 'mcp-quarantine.json');
  const verdict = { version: 1, servers: {
    'adp-tools': { fails: 3, last_status: 'error', last_error: 'timeout', last_failed_at: 1793018073.4, crossed_at: 1793018073.4 },
    other: { fails: 1, last_status: 'error', crossed_at: 0 } } };

  it('removes only the adp-tools entry and journals the verdict it discarded', async () => {
    mkdirSync(home, { recursive: true });
    writeFileSync(file(), JSON.stringify(verdict));
    const reported: string[] = [];
    await clearToolsQuarantine(home, line => reported.push(line));
    expect(JSON.parse(readFileSync(file(), 'utf8'))).toEqual({ version: 1, servers: { other: verdict.servers.other } });
    expect(reported.map(line => JSON.parse(line))).toEqual([{ event: 'crew_tools_quarantine_cleared', server: 'adp-tools',
      fails: 3, crossed_at: 1793018073.4, last_error: 'timeout' }]);
  });

  it('leaves a missing file or a file without the entry untouched', async () => {
    mkdirSync(home, { recursive: true });
    const reported: string[] = [];
    await clearToolsQuarantine(home, line => reported.push(line));
    expect(() => statSync(file())).toThrow();
    writeFileSync(file(), JSON.stringify({ version: 1, servers: { other: verdict.servers.other } }));
    const before = statSync(file()).mtimeMs;
    await clearToolsQuarantine(home, line => reported.push(line));
    expect(statSync(file()).mtimeMs).toBe(before);
    expect(reported).toEqual([]);
  });

  it('refuses to follow a symlinked quarantine file', async () => {
    mkdirSync(home, { recursive: true });
    writeFileSync(join(root, 'elsewhere.json'), JSON.stringify(verdict));
    symlinkSync(join(root, 'elsewhere.json'), file());
    await expect(clearToolsQuarantine(home)).rejects.toThrow();
    expect(JSON.parse(readFileSync(join(root, 'elsewhere.json'), 'utf8'))).toEqual(verdict);
  });
});
