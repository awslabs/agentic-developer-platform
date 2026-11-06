jest.mock('./operations-story-tools', () => ({
  ...jest.requireActual('./operations-story-tools'),
  invokeOperationsStoryTool: jest.fn().mockResolvedValue(JSON.stringify({ story_board: { state: 'absent' } })),
}));
jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  query: jest.fn(), createSdkMcpServer: jest.fn(({ tools }) => ({ tools })),
  tool: jest.fn((name) => ({ name })),
}));
jest.mock('./model-policy-runtime', () => ({
  prepareModelQuery: async (params: unknown) => params,
  ModelPolicyRefused: class extends Error {},
}));

import { query } from '@anthropic-ai/claude-agent-sdk';
import { createHash, generateKeyPairSync, KeyObject, randomUUID, sign } from 'node:crypto';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import * as http from 'node:http';
import { AddressInfo } from 'node:net';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { main } from './operations-story-entry';
import { ENVELOPE_HEADER } from './control-listener';
import { ENVELOPE_AUDIENCE, ENVELOPE_ISSUER, ENVELOPE_VERSION } from './control-envelope';
import * as revalidation from './control-revalidation';
import { invokeOperationsStoryTool } from './operations-story-tools';
import { savedBoard } from './__tests__/operations-board-fixture';
import { ExplanationEvents } from './explanation-events';
import { ClaudeProgress } from './claude-progress';
import * as controlFactory from './control-runtime-factory';
import { operationsQuery } from './operations-story-control';

const TOKEN = 'operations-control-test-token';

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

async function fixture() {
  const server = http.createServer();
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = (server.address() as AddressInfo).port;
  await new Promise<void>(resolve => server.close(() => resolve()));
  const directory = mkdtempSync(join(tmpdir(), 'operations-controls-'));
  const keys = generateKeyPairSync('ed25519');
  const previousEnv = process.env;
  process.env = {
    ...previousEnv,
    ADP_ENGINE_OPERATIONS_STORY: 'true', AGENT_TYPE: 'operations', ISSUE_NUMBER: '12',
    ADP_RULES_DIR: join(__dirname, '..', '..', 'rules'),
    ADP_OPERATIONS_STORY_CONTEXT: JSON.stringify({ title: 'Verify the release', workflows: ['deploy.yml'] }),
    ADP_OPERATIONS_REPORT_FILE: join(directory, 'report.json'), WORK_DIR: directory,
    FEATURE_AGENT_CONTROL_ENABLED: 'true', ADP_CONTROL_BIND_ADDRESS: '127.0.0.1',
    ADP_CONTROL_PORT: String(port), ADP_CONTROL_TOKEN: TOKEN, ADP_CONTROL_GENERATION: '1',
    ADP_CONTROL_RUN_ID: 'operations-fixture',
    ADP_CONTROL_TOKEN_EXPIRES_AT: new Date(Date.now() + 600_000).toISOString(),
    ADP_POD_DEADLINE_AT: new Date(Date.now() + 600_000).toISOString(),
    ADP_CONTROL_ENVELOPE_KEYS: JSON.stringify({ fixture: keys.publicKey.export({ type: 'spki', format: 'pem' }) }),
  };
  return {
    port, keys, directory,
    restore: () => { process.env = previousEnv; rmSync(directory, { recursive: true, force: true }); },
  };
}

function request(port: number, path: string, body?: string, envelope?: string, token = TOKEN) {
  return new Promise<{ status: number; body: any }>((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, path, method: body ? 'POST' : 'GET', headers: {
      Authorization: `Bearer ${token}`, 'content-type': 'application/json',
      'x-adp-control-generation': '1', ...(envelope ? { [ENVELOPE_HEADER]: envelope } : {}),
    } }, res => {
      let data = '';
      res.on('data', chunk => { data += chunk; });
      res.on('end', () => resolve({ status: res.statusCode!, body: JSON.parse(data) }));
    });
    req.on('error', reject);
    req.end(body);
  });
}

function signedEnvelope(body: string, action: string, commandId: string, privateKey: KeyObject) {
  const now = Date.now();
  const payload = Buffer.from(JSON.stringify({
    v: ENVELOPE_VERSION, iss: ENVELOPE_ISSUER, aud: ENVELOPE_AUDIENCE, alg: 'ed25519', kid: 'fixture',
    tenant_id: 'fixture', principal: 'operator', authority_kind: 'human_session', target_run_id: 'operations-fixture',
    target_generation: 1, action, command_id: commandId, body_digest: createHash('sha256').update(body).digest('hex'),
    iat: new Date(now).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    nbf: new Date(now).toISOString().replace(/\.\d{3}Z$/, 'Z'),
    exp: new Date(now + 30_000).toISOString().replace(/\.\d{3}Z$/, 'Z'),
  }));
  const signature = sign(null, Buffer.concat([Buffer.from(`${ENVELOPE_VERSION}.`), payload]), privateKey);
  return `${ENVELOPE_VERSION}.${payload.toString('base64url')}.${signature.toString('base64url')}`;
}

afterEach(() => { (query as jest.Mock).mockReset(); jest.restoreAllMocks(); });

it('persists Claude task progress exactly once when control startup fails', async () => {
  const test = await fixture();
  process.env.ADP_STORY_CHECKPOINT_FILE = join(test.directory, 'private.json');
  jest.spyOn(controlFactory, 'startControlRuntime').mockRejectedValue(new Error('offline fixture'));
  (invokeOperationsStoryTool as jest.Mock).mockResolvedValueOnce(JSON.stringify({ story_board: savedBoard(), story_checkpoint: {
    state: 'absent', scope: { tenant_id: 'org', flow_id: 'flow', node_id: 'node', run_id: 'operations-fixture',
      issue: 12, repository_id: 7, installation_id: 1, repository: 'example/service', branch: 'agent/issue-12',
      accepted_plan_version: 1, attempt: 1, claim_generation: 1 },
  } }));
  (query as jest.Mock).mockImplementation(() => (async function* () {
    yield { type: 'assistant', message: { content: [{ type: 'tool_use', name: 'TodoWrite', input: {
      todos: [{ content: 'AC-t1 [done] Verified result', status: 'completed' }],
    } }] } };
  })());
  try {
    for await (const _message of operationsQuery({ prompt: 'Continue story' })) { /* drain */ }
    const saved = JSON.parse(readFileSync(process.env.ADP_STORY_CHECKPOINT_FILE!, 'utf8'));
    expect(saved.story_checkpoint.delta.updates['AC-t1'].status).toBe('done');
    expect(saved.story_checkpoint.delta.history).toHaveLength(1);
  } finally { test.restore(); }
});

it.each(['true', 'false'])('streams the shared checklist and public SDK activity with controls enabled=%s', async enabled => {
  const test = await fixture();
  process.env.FEATURE_AGENT_EXPLANATIONS_ENABLED = 'true';
  process.env.FEATURE_AGENT_CONTROL_ENABLED = enabled;
  const ready = deferred();
  const finish = deferred();
  (query as jest.Mock).mockImplementation(({ prompt }) => Object.assign((async function* () {
    if (typeof prompt !== 'string') await prompt[Symbol.asyncIterator]().next();
    yield { type: 'stream_event', event: { type: 'message_start', message: { id: 'progress' } } };
    yield { type: 'stream_event', event: { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } } };
    yield { type: 'stream_event', event: { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: 'Checking the deployment. More detail follows' } } };
    yield { type: 'assistant', message: { id: 'progress', content: [
      { type: 'thinking', thinking: 'PRIVATE_REASONING' },
      { type: 'tool_use', id: 'plan', name: 'TodoWrite', input: { todos: [
        { content: 'Diagnose the failed verification', status: 'completed' },
        { content: 'Verify the deployed release', status: 'in_progress' },
      ] } },
      { type: 'tool_use', id: 'actions', name: 'mcp__pipeline-actions__actions_run', input: { private: 'PRIVATE_TOOL_INPUT' } },
    ] } };
    yield { type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: 'actions', content: 'PRIVATE_TOOL_RESULT' }] } };
    ready.resolve();
    await finish.promise;
    yield { type: 'result', subtype: 'success', result: 'Published the observed evidence.' };
  })(), { close: jest.fn() }));
  const running = main();
  let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
  try {
    await Promise.race([ready.promise, running.then(() => { throw new Error('Query ended before streaming'); })]);
    const response = await fetch(`http://127.0.0.1:${test.port}/agent/events`, { signal: AbortSignal.timeout(5000), headers: {
      Authorization: `Bearer ${TOKEN}`, 'x-adp-control-generation': '1',
    } });
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('text/event-stream');
    reader = response.body!.getReader();
    let text = '';
    while (!text.includes('mcp__pipeline-actions__actions_run completed')) {
      const chunk = await reader.read();
      if (chunk.done) throw new Error('Feed ended before the live activity arrived');
      text += new TextDecoder().decode(chunk.value);
    }
    expect(text).toContain('Checking the deployment.');
    expect(text).toContain('1 of 2 tasks complete');
    expect(text).toContain('Verify the deployed release');
    expect(text).toContain('"category":"plan"');
    expect(text).not.toMatch(/PRIVATE_REASONING|PRIVATE_TOOL_INPUT|PRIVATE_TOOL_RESULT/);
    finish.resolve();
    await running;
    for (;;) {
      const chunk = await reader.read();
      if (chunk.done) break;
      text += new TextDecoder().decode(chunk.value);
    }
    expect(text).toContain('event: terminal');
    expect(query).toHaveBeenCalledTimes(1);
  } finally {
    finish.resolve();
    await reader?.cancel().catch(() => {});
    await running.catch(() => {});
    test.restore();
  }
}, 10000);

it('pauses and resumes the operations output through signed shared controls', async () => {
  const test = await fixture();
  process.env.FEATURE_AGENT_EXPLANATIONS_ENABLED = 'true';
  const ready = deferred(), produce = deferred(), produced = deferred(), delivered = deferred(), finish = deferred();
  const observed = jest.spyOn(ClaudeProgress.prototype, 'observe');
  jest.spyOn(revalidation, 'revalidateQueuedCommand').mockResolvedValue({ allowed: true });
  (query as jest.Mock).mockImplementation(({ prompt }) => Object.assign((async function* () {
    await prompt[Symbol.asyncIterator]().next();
    yield { type: 'assistant', message: { id: 'before', content: [{ type: 'text', text: 'Before pause.' }] } };
    ready.resolve();
    await produce.promise;
    produced.resolve();
    yield { type: 'assistant', message: { id: 'after', content: [{ type: 'text', text: 'After resume.' }] } };
    delivered.resolve();
    await finish.promise;
    yield { type: 'result', subtype: 'success', result: 'Resumed the same operation.' };
  })(), { close: () => finish.resolve() }));
  const running = main();
  async function command(action: string) {
    const id = randomUUID(), body = JSON.stringify({ command_id: id });
    return request(test.port, `/agent/${action}`, body, signedEnvelope(body, action, id, test.keys.privateKey));
  }
  try {
    await Promise.race([ready.promise, running.then(() => { throw new Error('Query ended before pause'); })]);
    expect([200, 202]).toContain((await command('pause')).status);
    let state;
    for (let n = 0; n < 100; n++) {
      state = (await request(test.port, '/agent/state')).body;
      if (state.state === 'paused') break;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    expect(state).toMatchObject({ state: 'paused' });
    produce.resolve();
    await produced.promise;
    await new Promise(resolve => setImmediate(resolve));
    expect(JSON.stringify(observed.mock.calls)).not.toContain('After resume.');
    expect([200, 202]).toContain((await command('resume')).status);
    await delivered.promise;
    expect(JSON.stringify(observed.mock.calls)).toContain('After resume.');
    finish.resolve();
    await running;
    expect(query).toHaveBeenCalledTimes(1);
  } finally {
    produce.resolve(); finish.resolve();
    await command('resume').catch(() => {});
    await running.catch(() => {});
    test.restore();
  }
}, 10000);

it('delivers signed steering through the actual operations entry point and closes its listener', async () => {
  const test = await fixture();
  const ready = deferred();
  const received = deferred<unknown>();
  const finish = deferred();
  const close = jest.fn();
  const authority = jest.spyOn(revalidation, 'revalidateQueuedCommand').mockResolvedValue({ allowed: true });
  (query as jest.Mock).mockImplementation(({ prompt }) => {
    async function* messages() {
      if (typeof prompt === 'string') {
        // The broken entry point takes this path: the SDK runs, but its
        // registered control port is unreachable.
        ready.resolve();
        await finish.promise;
        yield { type: 'result', subtype: 'success', result: 'No steering channel.' };
        return;
      }
      const input = prompt[Symbol.asyncIterator]();
      expect(JSON.stringify((await input.next()).value)).toContain('Assigned story: Verify the release');
      yield { type: 'assistant', message: { content: [] } };
      const next = Promise.race([input.next(), finish.promise.then(() => ({ done: true, value: undefined }))]);
      ready.resolve();
      received.resolve((await next).value);
      await finish.promise;
      yield { type: 'result', subtype: 'success', result: 'Received diagnostic context; verification remains pending.' };
    }
    return Object.assign(messages(), { close });
  });
  const running = main();
  try {
    await Promise.race([ready.promise, running.then(() => { throw new Error('Query ended before accepting input'); })]);
    expect((await request(test.port, '/agent/state', undefined, undefined, 'wrong')).status).toBe(401);
    expect((await request(test.port, '/agent/state')).body.capabilities.steer).toBe(true);
    const options = (query as jest.Mock).mock.calls[0][0].options;
    expect(options.tools).toEqual(['TodoWrite']);
    expect(options.allowedTools.every((name: string) => name === 'TodoWrite' || name.startsWith('mcp__pipeline-actions__'))).toBe(true);
    expect(options.hooks.PreToolUse).toBeDefined();
    const commandId = randomUUID();
    const body = JSON.stringify({ command_id: commandId, instruction: 'Use the retained failure evidence.' });
    expect((await request(test.port, '/agent/steer', body)).status).not.toBe(202);
    const envelope = signedEnvelope(body, 'steer', commandId, test.keys.privateKey);
    expect((await request(test.port, '/agent/steer', body, envelope)).status).toBe(202);
    expect(JSON.stringify(await received.promise)).toContain('Use the retained failure evidence.');
    expect(authority).toHaveBeenCalled();
    finish.resolve();
    await running;
    expect(query).toHaveBeenCalledTimes(1);
    expect(close).toHaveBeenCalled();
    expect(JSON.parse(readFileSync(join(test.directory, 'report.json'), 'utf8')).report).toContain('Received diagnostic context');
    await expect(request(test.port, '/agent/state')).rejects.toThrow();
  } finally {
    finish.resolve();
    await running.catch(() => {});
    test.restore();
  }
}, 15000);

it('closes a failed controlled query without starting an extra attempt', async () => {
  const test = await fixture();
  (query as jest.Mock).mockImplementation(() => Object.assign((async function* () {
    yield { type: 'assistant', message: { content: [{ type: 'text', text: 'Reading deployment evidence.' }] } };
    throw new Error('HTTP 503');
  })(), { close: jest.fn() }));
  try {
    await expect(main()).rejects.toThrow('Operations story did not complete');
    expect(query).toHaveBeenCalledTimes(1);
    expect(JSON.parse(readFileSync(join(test.directory, 'report.json'), 'utf8')).report).toContain('Reading deployment evidence.');
    await expect(request(test.port, '/agent/state')).rejects.toThrow();
  } finally { test.restore(); }
});

it('records a signed abort as cancellation and never starts another SDK attempt', async () => {
  const test = await fixture();
  const ready = deferred();
  const finish = deferred();
  jest.spyOn(revalidation, 'revalidateQueuedCommand').mockResolvedValue({ allowed: true });
  (query as jest.Mock).mockImplementation(({ prompt }) => Object.assign((async function* () {
    await prompt[Symbol.asyncIterator]().next();
    yield { type: 'assistant', message: { content: [{ type: 'text', text: 'Waiting for deployment.' }] } };
    ready.resolve();
    await finish.promise;
  })(), { close: () => finish.resolve(), interrupt: async () => finish.resolve() }));
  const running = main();
  const outcome = running.then(() => null, error => error);
  try {
    await Promise.race([ready.promise, running.then(() => { throw new Error('Query ended before abort'); })]);
    const commandId = randomUUID();
    const body = JSON.stringify({ command_id: commandId, reason: 'Stop the fixture.' });
    const envelope = signedEnvelope(body, 'abort', commandId, test.keys.privateKey);
    expect([200, 202]).toContain((await request(test.port, '/agent/abort', body, envelope)).status);
    expect(await outcome).toBeInstanceOf(Error);
    expect(query).toHaveBeenCalledTimes(1);
    const { report } = JSON.parse(readFileSync(join(test.directory, 'report.json'), 'utf8'));
    expect(report).toContain('error_cancelled');
    expect(report).toContain('control_cancelled');
    await expect(request(test.port, '/agent/state')).rejects.toThrow();
  } finally {
    finish.resolve();
    await outcome;
    test.restore();
  }
}, 10000);


it('restores the saved plan before the first Claude model step and merges current task updates', async () => {
  const test = await fixture();
  process.env.TARGET_REPO = 'example/service';
  process.env.FEATURE_AGENT_EXPLANATIONS_ENABLED = 'true';
  (invokeOperationsStoryTool as jest.Mock).mockResolvedValueOnce(JSON.stringify({ story_board: savedBoard() }));
  const events = jest.spyOn(ExplanationEvents.prototype, 'publish');
  (query as jest.Mock).mockImplementation(({ prompt }) => Object.assign((async function* () {
    expect(events.mock.calls.some(([text, detail]) => detail?.category === 'plan' && text.includes('AC-c1'))).toBe(true);
    expect(JSON.stringify((await prompt[Symbol.asyncIterator]().next()).value)).toContain('Original completion evidence');
    yield { type: 'assistant', message: { id: 'progress', content: [
      { type: 'tool_use', id: 'generic', name: 'TodoWrite', input: { todos: [{ content: 'Replace with generic stages', status: 'pending' }] } },
      { type: 'tool_use', id: 'update', name: 'TodoWrite', input: { todos: [{ content: 'AC-i1 [done] Deploy', status: 'completed' }] } },
    ] } };
    yield { type: 'result', subtype: 'success', result: 'Saved tasks continued.' };
  })(), { close: jest.fn() }));
  try {
    await main();
    const plans = events.mock.calls.filter(([, detail]) => detail?.category === 'plan').map(([text]) => text);
    expect(plans).toHaveLength(2);
    expect(plans[1]).toContain('☑ `infra` AC-i1');
    expect(plans[1]).toContain('⛔ `test` AC-t1');
    expect(plans[1]).not.toContain('generic stages');
  } finally { test.restore(); }
});
