import { EventEmitter } from 'node:events';
import { chmodSync, mkdirSync, mkdtempSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import type { ChildProcess } from 'node:child_process';
import { currentCrewHostModel, currentCrewModelToken, currentCrewModelTokenExpiry, probeClaudeBackend, startCrewHostModel, verifyCrewHostModelProfile, TOOLS_WARMUP_GIVE_UP_MS, TOOLS_WARMUP_RETRY_MS, TOOLS_WARMUP_WAIT_MS, warmCrewTools, seedCrewHomeSkills } from './crew-host-model-entrypoint';
import type { currentCrewHostAuthority } from './crew-host-entrypoint';

const runId = '11111111-2222-4333-8444-555555555555';
const modelId = 'us.anthropic.example-v1:0';
const gatewayUrl = 'https://exampleapi.execute-api.us-east-1.amazonaws.com/dev';
const config = { run_id: runId, lease_generation: 2,
  gateway_execute_arn: 'arn:aws:execute-api:us-east-1:000000000000:exampleapi/dev', model_id: modelId };

it('accepts only root-managed model policy matching the verified current host receipt', () => {
  const directory = mkdtempSync(join(tmpdir(), 'crew-model-'));
  const file = join(directory, 'model.json');
  const authority = () => ({ runId, generation: 2 } as ReturnType<typeof currentCrewHostAuthority>);
  try {
    writeFileSync(file, JSON.stringify(config), { mode: 0o640 });
    const read = () => currentCrewHostModel(file, authority, process.getuid!());
    expect(read()).toEqual({ runId, modelId, region: 'us-east-1', gatewayUrl });
    writeFileSync(file, JSON.stringify({ ...config, run_id: '00000000-0000-0000-0000-000000000000' }));
    expect(read).toThrow('stale');
    writeFileSync(file, JSON.stringify(config));
    chmodSync(file, 0o666);
    expect(read).toThrow('untrusted');
    chmodSync(file, 0o640);
    writeFileSync(file, JSON.stringify({ ...config, gateway_execute_arn: 'https://elsewhere.example' }));
    expect(read).toThrow();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

it('boots Crew with only an ephemeral loopback key and refuses model calls after rotation', async () => {
  let active = true;
  const send = jest.fn(async (url: string | URL | Request) => String(url).includes('/model-token/refresh')
    ? new Response(null, { status: 204 }) : new Response(JSON.stringify({ content: [{ type: 'text', text: 'yes' }],
    stop_reason: 'end_turn', usage: { input_tokens: 2, output_tokens: 1 } })));
  let environment: NodeJS.ProcessEnv = {};
  let command: string[] = [];
  const renderTools = jest.fn(async () => undefined);
  const reported: string[] = [];
  const probeBackend = jest.fn(async (env: NodeJS.ProcessEnv) => [
    `probe saw ${env.CLAUDE_CODE_EXECUTABLE}`, 'Crew host model: claude-agent-acp --version failed (ENOENT)']);
  const child = Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess;
  const publishShell = jest.fn(async (payload: object) => {
    expect(payload).toEqual({ run_id: runId, model_id: modelId, base_url: expect.stringMatching(/^http:\/\/127\.0\.0\.1:/), secret: expect.stringMatching(/^[0-9a-f]{64}$/),
      model_aliases: { opus: 'global.anthropic.claude-opus-5-5', sonnet: 'global.anthropic.claude-sonnet-5', haiku: 'global.anthropic.claude-haiku-4-5-20251001-v1:0' } });
  });
  const removeShell = jest.fn(async () => undefined);
  const warmTools = jest.fn(async () => { expect(renderTools).toHaveBeenCalledTimes(1); expect(publishShell).not.toHaveBeenCalled(); return 200; });
  const clearQuarantine = jest.fn(async (home: string) => { expect(home).toBe('/mnt/crew'); expect(command).toEqual([]); });
  const seedSkills = jest.fn(async (home: string) => { expect(home).toBe('/mnt/crew'); expect(command).toEqual([]); });
  const supervisor = await startCrewHostModel({ refreshAws: async (env) => {
    expect(env).toMatchObject({ AWS_CONFIG_FILE: "/run/adp-crew-aws/config", AWS_EC2_METADATA_DISABLED: "true", AWS_SHARED_CREDENTIALS_FILE: "/dev/null" });
  }, probeBackend, warmTools, clearQuarantine, seedSkills, codexConfig: async () => undefined, guidance: async () => undefined, report: line => reported.push(line), config: () => {
    if (!active) throw new Error('rotated');
    return { runId, modelId, region: 'us-east-1', gatewayUrl };
  }, publishShell, removeShell, credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
  send, modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', verifyProfile: () => modelId, renderTools, ensureGhWrapper: async () => undefined,
  launch: (env, args) => { environment = env; command = args; return child; } });
  try {
    // #8380: the ADP tools shim is registered for this boot's adapter and run before Crew starts.
    // Claude Code's /model opus|sonnet|haiku resolve to gateway ids the owner is entitled to (dev 2026-10-25).
    expect(environment).toMatchObject({ ANTHROPIC_DEFAULT_OPUS_MODEL: 'global.anthropic.claude-opus-5-5',
      ANTHROPIC_DEFAULT_SONNET_MODEL: 'global.anthropic.claude-sonnet-5', ANTHROPIC_DEFAULT_HAIKU_MODEL: 'global.anthropic.claude-haiku-4-5-20251001-v1:0' });
    expect(publishShell).toHaveBeenCalledWith(expect.objectContaining({ model_aliases: { opus: 'global.anthropic.claude-opus-5-5',
      sonnet: 'global.anthropic.claude-sonnet-5', haiku: 'global.anthropic.claude-haiku-4-5-20251001-v1:0' } }));
    expect(renderTools).toHaveBeenCalledWith('/mnt/crew', {
      command: '/opt/adp/crew/current/venv/bin/python3', args: ['/opt/adp/crew/current/host/adp/adp-mcp.py'],
      env: { ADP_CREW_TOOLS_URL: supervisor.adapter.baseUrl, ADP_CREW_TOOLS_TOKEN: supervisor.adapter.secret, ADP_CREW_RUN_ID: runId },
    });
    expect(environment.ANTHROPIC_BASE_URL).toBe(supervisor.adapter.baseUrl);
    // #9589: a dashboard session's Claude Code runs the box CLI in host-run mode and finds the seeded skills.
    expect(environment.PATH).toMatch(/^\/opt\/adp\/crew\/current\/host:\/opt\/adp\/crew\/current\/host\/adp:/);
    expect(environment).toMatchObject({ ADP_CREW_TOOLS_URL: supervisor.adapter.baseUrl, ADP_CREW_TOOLS_TOKEN: supervisor.adapter.secret, ADP_CREW_RUN_ID: runId });
    expect(seedSkills).toHaveBeenCalledWith('/mnt/crew');
    expect(environment.ANTHROPIC_API_KEY).toBe(supervisor.adapter.secret);
    expect(environment.ANTHROPIC_MODEL).toBe(modelId);
    // The bundle's Node must lead PATH: the Claude agent adapter is a `#!/usr/bin/env node` script (#8223 cause 31).
    expect(environment.PATH?.startsWith('/opt/adp/crew/current/host:')).toBe(true);
    expect(command).toEqual(['gateway', '--no-open', '--no-tunnel', '--no-crons', '--port', '5476']);
    // The Claude backend probe runs under the Crew environment, is reported line by line, and a
    // failing component never stops Kiro Crew from starting (the ACP attempt is the authority).
    expect(probeBackend).toHaveBeenCalledTimes(1);
    // #8835: the catalogue is warmed after the shim is registered and before Kiro Crew starts.
    expect(warmTools).toHaveBeenCalledTimes(1);
    expect(clearQuarantine).toHaveBeenCalledTimes(1);
    expect(reported.map(line => { try { return JSON.parse(line); } catch { return line; } })).toContainEqual(
      expect.objectContaining({ event: 'crew_tools_warmup', status: 200 }));
    expect(reported.slice(1)).toEqual(['probe saw /opt/adp/crew/current/clients/node_modules/.bin/claude',
      'Crew host model: claude-agent-acp --version failed (ENOENT)']);
    expect(environment.AWS_ACCESS_KEY_ID).toBeUndefined();
    const invoke = () => fetch(`${supervisor.adapter.baseUrl}/v1/messages`, { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-api-key': supervisor.adapter.secret },
      body: JSON.stringify({ model: modelId, max_tokens: 20, messages: [{ role: 'user', content: 'hello' }] }) });
    expect((await invoke()).status).toBe(200);
    expect(send).toHaveBeenCalledTimes(4);
    active = false;
    const fencedClaude = await invoke();
    expect(fencedClaude.status).toBe(503);
    expect(await fencedClaude.json()).toMatchObject({ error: { type: 'api_error', message: 'Gateway unavailable' } });
    const fencedCodex = await fetch(`${supervisor.adapter.baseUrl}/v1/responses`, { method: 'POST', headers: {
      'content-type': 'application/json', authorization: `Bearer ${supervisor.adapter.secret}` },
    body: JSON.stringify({ model: modelId, input: 'hello' }) });
    expect(fencedCodex.status).toBe(503);
    expect(await fencedCodex.json()).toMatchObject({ error: { type: 'api_error', message: 'Gateway unavailable' } });
    expect(send).toHaveBeenCalledTimes(4);
  } finally { await supervisor.close(); }
  expect(child.kill).toHaveBeenCalledWith('SIGTERM');
  expect(publishShell).toHaveBeenCalledTimes(1);
  expect(removeShell).toHaveBeenCalledTimes(1);
});

it('refuses an unverified profile or a switched model instead of invoking a fallback backend', () => {
  const home = mkdtempSync(join(tmpdir(), 'crew-model-home-'));
  const agents = join(home, '.kiro', 'agents');
  const configFile = join(home, 'config.json');
  const agentFile = join(agents, 'kirocrew.json');
  try {
    mkdirSync(agents, { recursive: true });
    writeFileSync(configFile, JSON.stringify({ agent: { acp_backend: 'claude', member_acp_backend: 'claude', model: modelId },
      memory: { embed_model_path: '/opt/adp/crew/current/models/qwen3-embedding-0.6b-q8_0.gguf' } }));
    writeFileSync(agentFile, JSON.stringify({ name: 'kirocrew', model: modelId }));
    expect(verifyCrewHostModelProfile(home, modelId)).toBe(modelId);
    expect(() => verifyCrewHostModelProfile(home, 'another-model')).toThrow();
    writeFileSync(agentFile, JSON.stringify({ name: 'kirocrew', model: 'another-model' }));
    expect(() => verifyCrewHostModelProfile(home, modelId)).toThrow();
    rmSync(agentFile);
    symlinkSync(configFile, agentFile);
    expect(() => verifyCrewHostModelProfile(home, modelId)).toThrow();
    rmSync(agentFile);
    const external = mkdtempSync(join(tmpdir(), 'crew-other-agent-'));
    try {
      rmSync(agents, { recursive: true });
      symlinkSync(external, agents);
      writeFileSync(join(external, 'kirocrew.json'), JSON.stringify({ name: 'kirocrew', model: modelId }));
      expect(() => verifyCrewHostModelProfile(home, modelId)).toThrow('Untrusted');
    } finally { rmSync(external, { recursive: true, force: true }); }
  } finally { rmSync(home, { recursive: true, force: true }); }
});

test('host model reads only the current private token and rejects expired or foreign runs', () => {
  const directory = mkdtempSync(join(tmpdir(), 'crew-token-'));
  const tokenFile = join(directory, 'token.json');
  try {
    const payload = { run_id: runId, lease_generation: 2,
      token: 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', expires_at: 1_800_000_060 };
    writeFileSync(tokenFile, JSON.stringify(payload), { mode: 0o600 });
    const read = (id = runId, generation = 2, now = 1_800_000_000) =>
      currentCrewModelToken(id, generation, tokenFile, now, process.getuid!());
    expect(read()).toBe(payload.token);
    expect(() => read('00000000-0000-4000-8000-000000000000')).toThrow();
    expect(() => read(runId, 3)).toThrow();
    expect(() => read(runId, 2, payload.expires_at)).toThrow();
    chmodSync(tokenFile, 0o644);
    expect(read).toThrow('Untrusted');
    rmSync(tokenFile);
    symlinkSync(join(directory, 'elsewhere'), tokenFile);
    expect(read).toThrow();
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('a start-up refusal is named on stderr and a stale authority receipt is the named reason after a slow resume', async () => {
  // dev 2026-10-20: adp-crew.service restarted in a loop with an empty journal. The authority
  // receipt published at enrolment carried the run lease; readiness retries outlived it, so the
  // service refused "stale" at start — silently, because the entry point swallowed the error.
  const { describeHostModelFailure } = await import('./crew-host-model-entrypoint');
  const { currentCrewHostAuthority } = await import('./crew-host-trusted');
  const directory = mkdtempSync(join(tmpdir(), 'crew-authority-'));
  try {
    chmodSync(directory, 0o700);
    const uid = process.getuid!();
    const authority = { run_id: runId, tenant_id: 'tenant', user_id: 'owner', box_id: 'box', instance_ref: 'i-0123456789abcdef0',
      private_ip: '10.2.3.4', profile_revision: 'profile-r1', restore_manifest_version: 0, lease_generation: 2,
      lease_expires_at: 1_800_000_300, gateway_fingerprint: 'a'.repeat(64),
      verification_keys: { k1: '-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA3Og+curactMTP/SVmWcAy6XPt8qtlK7mxwzzmT0MCRs=\n-----END PUBLIC KEY-----\n' } };
    const { verification_keys: _keys, gateway_fingerprint: _fp, ...ready } = authority;
    const files = { authority: join(directory, 'crew-host.json'), ready: join(directory, 'crew-ready'), profile: join(directory, 'profile.json') };
    writeFileSync(files.authority, JSON.stringify(authority), { mode: 0o640 });
    writeFileSync(files.ready, JSON.stringify(ready), { mode: 0o640 });
    writeFileSync(files.profile, JSON.stringify({ revision: 'profile-r1' }), { mode: 0o644 });
    const read = (now: number) => currentCrewHostAuthority(files.authority, files.ready, files.profile, now, uid);
    expect(read(1_800_000_000).runId).toBe(runId);
    let refused: unknown;
    try { read(1_800_000_300); } catch (error) { refused = error; }
    expect(describeHostModelFailure(refused)).toBe('Crew host model refused: Error: Crew host authority is stale or unverified');
    // The service entry point reports whatever stopped the start-up, never an empty line.
    await expect(startCrewHostModel({ config: () => { throw new Error('Crew host model configuration is stale'); } }))
      .rejects.toThrow('Crew host model configuration is stale');
    expect(describeHostModelFailure(new Error('x\ny'))).toBe('Crew host model refused: Error: x y');
    expect(describeHostModelFailure('not an error')).toBe('Crew host model refused: Error');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('stopping the host cancels an in-flight owner shell call without an auth prompt or identity fallback', async () => {
  let started!: () => void;
  const modelStarted = new Promise<void>(resolve => { started = resolve; });
  const send = jest.fn(async (url: string | URL | Request, init?: RequestInit): Promise<Response> => {
    if (String(url).includes('/model-token/refresh')) return new Response(null, { status: 204 });
    started();
    return new Promise<Response>((_resolve, reject) => {
      init?.signal?.addEventListener('abort', () => reject(new Error('synthetic model transport cancelled')), { once: true });
    });
  });
  const child = Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess;
  const supervisor = await startCrewHostModel({ refreshAws: async (env) => {
    expect(env).toMatchObject({ AWS_CONFIG_FILE: "/run/adp-crew-aws/config", AWS_EC2_METADATA_DISABLED: "true", AWS_SHARED_CREDENTIALS_FILE: "/dev/null" });
  }, config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send, modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', verifyProfile: () => modelId,
    renderTools: async () => undefined, ensureGhWrapper: async () => undefined, publishShell: async () => undefined, removeShell: async () => undefined,
    probeBackend: async () => [], warmTools: async () => 200, clearQuarantine: async () => undefined, launch: () => child });
  const ongoing = fetch(`${supervisor.adapter.baseUrl}/v1/messages`, { method: 'POST', headers: {
    'content-type': 'application/json', 'x-api-key': supervisor.adapter.secret },
  body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'hello' }], max_tokens: 20 }) });
  await modelStarted;
  const closing = supervisor.close();
  const response = await ongoing;
  expect(response.status).toBe(503);
  const refusal = await response.json() as { error: { type: string; message: string } };
  expect(refusal.error).toMatchObject({ type: 'api_error', message: 'Gateway unavailable' });
  expect(JSON.stringify(refusal)).not.toMatch(/login|sign.in|expired/i);
  await closing;
  expect(child.kill).toHaveBeenCalledWith('SIGTERM');
  expect(send.mock.calls.filter(([url]) => String(url).includes('/agent/model/'))).toHaveLength(1);
});

test('failed owner credential publication closes the adapter without starting Crew', async () => {
  const removeShell = jest.fn(async () => undefined);
  const launch = jest.fn();
  await expect(startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => { throw new Error('synthetic publisher refused'); }, removeShell, launch, warmTools: async () => 200, clearQuarantine: async () => undefined,
  })).rejects.toThrow('synthetic publisher refused');
  expect(removeShell).toHaveBeenCalledTimes(1);
  expect(launch).not.toHaveBeenCalled();
});

test('the Crew home receives both clients\' ADP skills from the bundle installer, and a seeding failure is advisory (#9589)', async () => {
  const calls: Array<[string, string[]]> = [];
  await seedCrewHomeSkills('/mnt/crew', async (file, args) => { calls.push([file, args]); return undefined; });
  expect(calls).toEqual([
    ['/opt/adp/crew/current/venv/bin/python3', ['-I', '/opt/adp/crew/current/host/adp/adp_skills.py', '--client', 'claude-code',
      '--source', '/opt/adp/crew/current/host/adp/skills', '--home', '/mnt/crew']],
    ['/opt/adp/crew/current/venv/bin/python3', ['-I', '/opt/adp/crew/current/host/adp/adp_skills.py', '--client', 'codex',
      '--source', '/opt/adp/crew/current/host/adp/skills', '--home', '/mnt/crew']],
  ]);
  const reported: string[] = [];
  const launch = jest.fn(() => Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess);
  const supervisor = await startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => undefined, removeShell: async () => undefined, launch, warmTools: async () => 200,
    clearQuarantine: async () => undefined, probeBackend: async () => [], refreshAws: async () => undefined,
    seedSkills: async () => { throw new Error('synthetic skills installer refused'); }, report: line => reported.push(line),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send: jest.fn(async () => new Response(null, { status: 204 })), modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
  try {
    expect(launch).toHaveBeenCalledTimes(1);
    expect(reported.some(line => line.includes('crew_home_skills_unseeded') && line.includes('synthetic skills installer refused'))).toBe(true);
  } finally {
    await supervisor.close();
  }
});

test('Kiro Crew\'s environment names the adapter secret for Codex too, and the Crew home Codex config is written for this boot (#9628)', async () => {
  const reported: string[] = [];
  let environment: NodeJS.ProcessEnv = {};
  const codexWrites: Array<[string, { modelId: string; baseUrl: string }]> = [];
  const launch = jest.fn(() => Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess);
  const supervisor = await startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => undefined, removeShell: async () => undefined, launch, warmTools: async () => 200,
    clearQuarantine: async () => undefined, probeBackend: async () => [], refreshAws: async env => { environment = env; },
    seedSkills: async () => undefined, codexConfig: async (home, settings) => { codexWrites.push([home, settings]); }, guidance: async () => undefined,
    report: line => reported.push(line),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send: jest.fn(async () => new Response(null, { status: 204 })), modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
  try {
    expect(environment.OPENAI_API_KEY).toBeTruthy();
    expect(environment.OPENAI_API_KEY).toBe(environment.ANTHROPIC_API_KEY);
    expect(codexWrites).toEqual([['/mnt/crew', { modelId, baseUrl: environment.ANTHROPIC_BASE_URL }]]);
    expect(reported.some(line => line.includes('crew_home_codex_unconfigured'))).toBe(false);
  } finally {
    await supervisor.close();
  }
});

test('a Codex config write failure is advisory: journaled, and Kiro Crew still starts (#9628)', async () => {
  const reported: string[] = [];
  const launch = jest.fn(() => Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess);
  const supervisor = await startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => undefined, removeShell: async () => undefined, launch, warmTools: async () => 200,
    clearQuarantine: async () => undefined, probeBackend: async () => [], refreshAws: async () => undefined,
    seedSkills: async () => undefined, codexConfig: async () => { throw new Error('synthetic codex config refused'); }, guidance: async () => undefined,
    report: line => reported.push(line),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send: jest.fn(async () => new Response(null, { status: 204 })), modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
  try {
    expect(launch).toHaveBeenCalledTimes(1);
    expect(reported.some(line => line.includes('crew_home_codex_unconfigured') && line.includes('synthetic codex config refused'))).toBe(true);
  } finally {
    await supervisor.close();
  }
});

test('the Claude backend probe names each component, bounds its output and never leaks the adapter secret', async () => {
  const environment: NodeJS.ProcessEnv = {
    CLAUDE_CODE_EXECUTABLE: '/opt/adp/crew/current/clients/node_modules/.bin/claude',
    CLAUDE_AGENT_ACP_BIN: '/opt/adp/crew/current/clients/node_modules/.bin/claude-agent-acp',
    ANTHROPIC_API_KEY: 'a'.repeat(64), HOME: '/mnt/crew',
  };
  const calls: Array<[string, string[]]> = [];
  const runner = async (file: string, args: string[], env: NodeJS.ProcessEnv) => {
    calls.push([file, args]);
    expect(env).toBe(environment);
    if (file.endsWith('/claude')) return `2.1.296 (Claude Code)\nsecond line ${'x'.repeat(300)}`;
    throw Object.assign(new Error(`spawn ${file} ENOENT  with   spaces`), { code: 'ENOENT' });
  };
  const lines = await probeClaudeBackend(environment, runner);
  expect(calls).toEqual([[environment.CLAUDE_CODE_EXECUTABLE, ['--version']], [environment.CLAUDE_AGENT_ACP_BIN, ['--version']]]);
  expect(lines[0]).toBe('Crew host model: claude --version -> 2.1.296 (Claude Code)');
  expect(lines[1]).toMatch(/^Crew host model: claude-agent-acp --version failed \(spawn .*ENOENT with spaces\)$/);
  expect(lines.join('\n')).not.toContain('a'.repeat(64));
  expect(await probeClaudeBackend({}, runner)).toEqual([
    'Crew host model: claude not configured', 'Crew host model: claude-agent-acp not configured']);
});


test('the token expiry reader shares the token reader\'s trust checks and returns expires_at (#8223 Dev Box tool latency)', () => {
  const directory = mkdtempSync(join(tmpdir(), 'crew-token-expiry-'));
  chmodSync(directory, 0o700);
  const tokenFile = join(directory, 'token.json');
  const id = '11111111-2222-4333-8444-555555555555';
  writeFileSync(tokenFile, JSON.stringify({ run_id: id, lease_generation: 3, token: 'eyJ0ZXN0IjoidG9rZW4ifQ.signature', expires_at: 1_800_000_060 }), { mode: 0o600 });
  try {
    expect(currentCrewModelTokenExpiry(id, 3, tokenFile, 1_800_000_000, process.getuid!())).toBe(1_800_000_060);
    expect(() => currentCrewModelTokenExpiry(id, 3, tokenFile, 1_800_000_060, process.getuid!())).toThrow('expired or replaced');
    expect(() => currentCrewModelTokenExpiry(id, 4, tokenFile, 1_800_000_000, process.getuid!())).toThrow('expired or replaced');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});


describe('tools warm-up (#8835)', () => {
  const parsed = (lines: string[]) => lines.map(line => JSON.parse(line));
  const flush = () => new Promise(resolve => setTimeout(resolve, 5));

  it('journals the first refusal, keeps retrying until the host is bound, then journals the success', async () => {
    const reported: string[] = [];
    const warm = jest.fn<Promise<number>, []>()
      .mockRejectedValueOnce(new Error('Chat data denied (HTTP 403)'))
      .mockResolvedValueOnce(403)
      .mockResolvedValue(200);
    await warmCrewTools(warm, line => reported.push(line), { waitMs: 500, retryMs: 5 });
    expect(warm).toHaveBeenCalledTimes(3);
    expect(parsed(reported)).toEqual([
      expect.objectContaining({ event: 'crew_tools_warmup', error: 'Chat data denied (HTTP 403)', attempt: 1 }),
      expect.objectContaining({ event: 'crew_tools_warmup', status: 200, attempt: 3 })]);
  });

  it('stops waiting after the bound while the loop keeps running, then journals its outcome', async () => {
    const reported: string[] = [];
    let finish!: (status: number) => void;
    const slow = new Promise<number>(resolve => { finish = resolve; });
    await warmCrewTools(() => slow, line => reported.push(line), { waitMs: 20 });
    expect(parsed(reported)).toEqual([{ event: 'crew_tools_warmup_pending', waited_ms: 20 }]);
    finish(200);
    await flush();
    expect(parsed(reported).at(-1)).toEqual(expect.objectContaining({ event: 'crew_tools_warmup', status: 200, attempt: 1 }));
  });

  it('gives up after the deadline and journals the abandonment once', async () => {
    const reported: string[] = [];
    const warm = jest.fn(async () => 403);
    await warmCrewTools(warm, line => reported.push(line), { waitMs: 500, retryMs: 5, giveUpMs: 40 });
    const events = parsed(reported).map(line => line.event);
    expect(events[0]).toBe('crew_tools_warmup');
    expect(events.at(-1)).toBe('crew_tools_warmup_abandoned');
    expect(events.filter(event => event === 'crew_tools_warmup_abandoned')).toHaveLength(1);
    expect(warm.mock.calls.length).toBeGreaterThan(1);
  });

  it('stops quietly when the host is closing', async () => {
    const reported: string[] = [];
    const controller = new AbortController();
    const warm = jest.fn(async () => { controller.abort(); return 503; });
    await warmCrewTools(warm, line => reported.push(line), { waitMs: 500, retryMs: 5, signal: controller.signal });
    expect(warm).toHaveBeenCalledTimes(1);
    expect(parsed(reported).map(line => line.event)).toEqual(['crew_tools_warmup']);
    expect([TOOLS_WARMUP_WAIT_MS, TOOLS_WARMUP_RETRY_MS, TOOLS_WARMUP_GIVE_UP_MS]).toEqual([10_000, 5_000, 300_000]);
  });
});


test('the Crew home guidance is written for this home and region, and a failure is advisory (#9655 AC-03)', async () => {
  const reported: string[] = [];
  const guidanceWrites: Array<[string, { region: string }]> = [];
  const launch = jest.fn(() => Object.assign(new EventEmitter(), { kill: jest.fn(() => true) }) as unknown as ChildProcess);
  const supervisor = await startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => undefined, removeShell: async () => undefined, launch, warmTools: async () => 200,
    clearQuarantine: async () => undefined, probeBackend: async () => [], refreshAws: async () => undefined,
    seedSkills: async () => undefined, codexConfig: async () => undefined,
    guidance: async (home, settings) => { guidanceWrites.push([home, settings]); },
    report: line => reported.push(line),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send: jest.fn(async () => new Response(null, { status: 204 })), modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
  try {
    expect(guidanceWrites).toEqual([['/mnt/crew', { region: 'us-east-1' }]]);
    expect(reported.some(line => line.includes('crew_home_guidance_unwritten'))).toBe(false);
  } finally {
    await supervisor.close();
  }
  const failing = await startCrewHostModel({ config: () => ({ runId, modelId, region: 'us-east-1', gatewayUrl }),
    verifyProfile: () => modelId, renderTools: async () => undefined, ensureGhWrapper: async () => undefined,
    publishShell: async () => undefined, removeShell: async () => undefined, launch, warmTools: async () => 200,
    clearQuarantine: async () => undefined, probeBackend: async () => [], refreshAws: async () => undefined,
    seedSkills: async () => undefined, codexConfig: async () => undefined,
    guidance: async () => { throw new Error('synthetic guidance refused'); },
    report: line => reported.push(line),
    credentials: async () => ({ accessKeyId: 'TESTACCESSKEY', secretAccessKey: 'synthetic', sessionToken: 'temporary' }),
    send: jest.fn(async () => new Response(null, { status: 204 })), modelToken: async () => 'eyJ0ZXN0IjoidG9rZW4ifQ.signature' });
  try {
    expect(launch).toHaveBeenCalledTimes(2);
    expect(reported.some(line => line.includes('crew_home_guidance_unwritten') && line.includes('synthetic guidance refused'))).toBe(true);
  } finally {
    await failing.close();
  }
});
