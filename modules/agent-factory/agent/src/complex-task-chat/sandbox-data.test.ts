import { createHash } from 'node:crypto';
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { canonicalJson } from '../invocability-probe/canonical-json';
import { ChatDataClient } from './gateway/chat-data-client';
import { ChatModelRequest, modelRequestSchema } from './gateway/chat-model-contract';
import { SandboxDataRuntime } from './sandbox-data';
import { executeSandboxTurn } from './sandbox-turn';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const stamp = new Date(NOW).toISOString();
const scope = { run_id: 'run-a', session_id: 'session-a' };
const artifactId = 'art_0123456789ab';
const memoryId = `mem_${'a'.repeat(32)}`;
const accepted = {
  ref: `user_${createHash('sha256').update(scope.run_id).digest('hex')}`,
  message: { role: 'user' as const, content: 'Review my own artifact', ts: stamp, tokens: 6,
    parts: [{ type: 'file' as const, artifactId }] },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function page(source: string, entries: unknown[], fields: Record<string, unknown> = {}) {
  return { status: entries.length ? 'ok' : 'empty', entries, next_cursor: null, observed_at: stamp,
    coverage: { source, complete: true, missing_source_ids: [] }, ...fields };
}

describe('sandbox data wiring through real scoped clients and workspace files', () => {
  let workspace: string;
  let data: SandboxDataRuntime;
  let client: ChatDataClient;
  let modelResponses: Record<string, unknown>[];
  let cancellation: AbortController;
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let refusedPath: string;
  let failureStatus: number;
  let history: Array<{ ref: string; role: 'user' | 'assistant'; content: string }>;
  let version: number;
  let storedMemory: Record<string, unknown>;

  beforeEach(async () => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    workspace = await mkdtemp(path.join(os.tmpdir(), 'sandbox-scoped-data-'));
    refusedPath = '';
    modelResponses = [];
    cancellation = new AbortController();
    failureStatus = 404;
    history = [{ ref: 'prior-message', role: 'assistant', content: 'Previous owned reply' },
      { ref: accepted.ref, role: 'user', content: accepted.message.content }];
    version = 2;
    storedMemory = {
      id: memoryId, version: 1, content: 'Owner preference', kind: 'preference', tags: [], labels: {},
      scope: { user: 'owner', tenant: 'tenant' }, source: { sessionId: scope.session_id, runId: scope.run_id },
      createdAt: stamp, updatedAt: stamp,
    };
    fetchMock = jest.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
      const route = new URL(url as string).pathname;
      const body = options?.body ? JSON.parse(options.body as string) : {};
      if (route === refusedPath) return json({ detail: { error: 'chat_scope_refused' } }, failureStatus);
      if (route.endsWith('/bootstrap')) return json({ ...scope, capability: 'synthetic.capability',
        lease_generation: 1, attempt: 1, expires_at: NOW / 1000 + 300 });
      expect(options?.headers).toMatchObject({ Authorization: 'Bearer synthetic.capability', 'X-Adp-Workload-Token': 'sandbox.token' });
      if (route.includes('/artifact/session-a/')) {
        expect(new URL(url as string).searchParams.get('run_id')).toBe(scope.run_id);
        return new Response('Attached owned content', { headers: { 'Content-Type': 'text/plain', 'X-Artifact-Scan-Status': 'clean' } });
      }
      expect(body.run_id).toBe(scope.run_id);
      if (!route.includes('/memory/')) expect(body.session_id).toBe(scope.session_id);
      if (route.endsWith('/memory/search') || route.endsWith('/memory/read')) return json(page('owned_memory', [storedMemory]));
      if (route.endsWith('/memory/write')) {
        storedMemory = { ...storedMemory, content: body.content, kind: body.kind, tags: body.tags, labels: body.labels };
        return json({ memory_id: memoryId, version: body.expected_version + 1 });
      }
      if (route.endsWith('/history/turn')) return json({ message_id: accepted.ref, ordinal: history.findIndex(item => item.ref === accepted.ref) + 1 });
      if (route.endsWith('/history/read')) return json(page('session_context', history.slice(0, body.limit).map((item, index) => ({
        ordinal: index + 1, type: 'msg', ref: item.ref, tokens: Math.ceil(item.content.length / 4),
      })), { version }));
      if (route.endsWith('/history/messages')) return json(page('session_context', body.ids.map((reference: string) => {
        const message = history.find(item => item.ref === reference)!;
        return { ref: reference, message: { role: message.role, content: message.content, ts: stamp, tokens: Math.ceil(message.content.length / 4) } };
      })));
      if (route.endsWith('/history/summary')) return json(page('session_context', [{ ref: body.summary_id, summary: {
        depth: 0, kind: 'leaf', content: 'An owned summary', sourceIds: ['prior-message'], parentIds: [],
        earliestAt: stamp, latestAt: stamp, tokens: 4,
      } }]));
      if (route.endsWith('/history/append')) {
        if (!history.some(item => item.ref === 'assistant-result')) {
          history.push({ ref: 'assistant-result', role: 'assistant', content: body.content });
          version += 1;
        }
        return json({ message_id: 'assistant-result', ordinal: history.length, version: body.expected_version + 1 });
      }
      if (route.endsWith('/turn/result')) return json({ ...scope, attempt: 1, lease_generation: 1,
        sandbox_uid: 'sandbox-a', outcome: body.outcome, message_id: body.message_id ?? null, terminal: false });
      if (route.endsWith('/history/compact')) return json({ summary_id: 'summary-new', version: body.expected_version + 1 });
      if (route.endsWith('/model/invoke')) return json({ ...scope, operation_id: body.operation_id, lease_generation: 1,
        request_digest: createHash('sha256').update(canonicalJson(body.request)).digest('hex'), model_id: 'approved-model',
        status: 'confirmed', handoff: 'confirmed', reservation_status: 'settled', automatic_replay_permitted: false,
        content: [{ type: 'text', text: 'Owned history, preserving source references' }], stop_reason: 'end_turn',
        usage: { input_tokens: 10, output_tokens: 8, estimated_usd: '0.01' },
        ...modelResponses.shift(),
      });
      if (route.endsWith('/artifact/create')) return json({
        id: artifactId, filename: body.filename, contentType: body.content_type,
        sizeBytes: Buffer.from(body.content_base64, 'base64').length, checksum: body.content_sha256,
        source: 'agent', createdAt: stamp, scanStatus: 'clean', url: '/scoped-download', urlExpiresAt: stamp,
      });
      if (route.endsWith('/artifact/list')) return json(page('session_artifacts', []));
      throw new Error(`Unexpected fixture route: ${route}`);
    });
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.token', signal: cancellation.signal });
    data = new SandboxDataRuntime(client, scope, accepted, workspace);
  });

  afterEach(async () => {
    jest.restoreAllMocks();
    await rm(workspace, { recursive: true, force: true });
  });

  it('runs native model/tool rounds and saves the owner reply only after confirmed, settled inference', async () => {
    modelResponses.push({ content: [
      { type: 'tool_use', id: 'call-memory', name: 'read_memory', input: { id: memoryId } },
      { type: 'tool_use', id: 'call-summary', name: 'expand_summary', input: { summary_id: 'owned-summary' } },
    ], stop_reason: 'tool_use' }, { content: [
      { type: 'tool_use', id: 'call-artifact', name: 'publish_artifact', input: { path: 'report.txt' } },
    ], stop_reason: 'tool_use' });
    await writeFile(path.join(workspace, 'report.txt'), 'Owned result');
    await executeSandboxTurn({ client, data, turn: accepted, context: await data.prepare() }, cancellation.signal);
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'));
    expect(calls).toHaveLength(3);
    const requests = calls.map(([, options]) => JSON.parse(options?.body as string));
    expect(requests.map(request => request.operation_id)).toEqual([0, 1, 2].map(round => `turn_${accepted.ref.slice(5)}_${round}`));
    expect(requests[0].request.tools.map((tool: { name: string }) => tool.name)).toContain('publish_artifact');
    expect(requests[0].request.messages.at(-1).content).toContain('Owner preference');
    expect(requests[1].request.messages.at(-1)).toMatchObject({ role: 'user', content: [
      { type: 'tool_result', tool_use_id: 'call-memory', content: expect.stringContaining('Owner preference') },
      { type: 'tool_result', tool_use_id: 'call-summary', content: expect.stringContaining('Previous owned reply') },
    ] });
    expect(requests[2].request.messages.at(-1).content[0].content).toContain('/v1/chat/data/artifact/session-a/');
    expect(history.at(-1)).toMatchObject({ role: 'assistant', content: 'Owned history, preserving source references' });
    expect(fetchMock.mock.calls.every(([url]) => new URL(String(url)).origin === 'https://gateway.example.test')).toBe(true);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/history/append'))).toHaveLength(1);
    const resultCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/turn/result'));
    expect(resultCalls).toHaveLength(1);
    expect(JSON.parse(resultCalls[0][1]!.body as string)).toEqual({
      ...scope, outcome: 'completed', message_id: 'assistant-result',
    });
    expect(fetchMock.mock.calls.findIndex(([url]) => String(url).endsWith('/history/append')))
      .toBeLessThan(fetchMock.mock.calls.findIndex(([url]) => String(url).endsWith('/turn/result')));
  });

  it.each([32, 40])('fits %i short history messages while retaining the current turn and tool-round capacity', async count => {
    const prior = Array.from({ length: count }, (_, index) => ({
      ref: `message-${index}`, role: index % 2 ? 'assistant' as const : 'user' as const, content: `m${index}`,
    }));
    history = [...prior, { ref: accepted.ref, role: 'user', content: accepted.message.content }];
    modelResponses.push(...[0, 1].map(round => ({ content: [
      { type: 'tool_use', id: `read-${round}`, name: 'read_memory', input: { id: memoryId } },
    ], stop_reason: 'tool_use' })));
    const context = await data.prepare();
    expect(context.messages).toHaveLength(count);
    expect(context.meta.estimatedTokens).toBe(count);
    await executeSandboxTurn({ client, data, turn: accepted, context });
    const requests: ChatModelRequest[] = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))
      .map(([, options]) => JSON.parse(options?.body as string).request);
    expect(requests).toHaveLength(3);
    for (const [round, request] of requests.entries()) {
      const retained = 29 - round * 2;
      expect(modelRequestSchema.safeParse(request).success).toBe(true);
      expect(Buffer.byteLength(canonicalJson(request))).toBeLessThanOrEqual(65_536);
      expect(request.messages).toHaveLength(30);
      expect(request.messages.slice(0, retained)).toEqual(prior.slice(-retained).map(({ role, content }) => ({ role, content })));
      expect(JSON.parse(request.messages[retained].content as string)).toEqual({
        message: accepted.message.content, memories: context.memories, attachments: context.attachments,
      });
      expect(request.messages.slice(retained + 1).map(message => message.role)).toEqual(
        Array.from({ length: round }, () => ['assistant', 'user']).flat(),
      );
    }
    expect(history.slice(0, count)).toEqual(prior);
    expect(history.at(-1)).toMatchObject({ role: 'assistant', content: 'Owned history, preserving source references' });
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/history/compact'))).toBe(false);
  });

  it.each(['🧭'.repeat(750), '\u0001'.repeat(500)])('fits canonical UTF-8 frames including schemas and tool exchanges (%#)', async content => {
    const prior = Array.from({ length: 24 }, (_, index) => ({ ref: `message-${index}`, role: 'user' as const, content }));
    history = [...prior, { ref: accepted.ref, role: 'user', content: accepted.message.content }];
    const context = await data.prepare();
    expect(context.messages).toHaveLength(24);
    const toolText = '回'.repeat(2400);
    jest.spyOn(data, 'executeTool').mockResolvedValue({ content: [{ type: 'text', text: toolText }] });
    modelResponses.push({ content: [
      { type: 'tool_use', id: 'read-large', name: 'read_memory', input: { id: memoryId } },
    ], stop_reason: 'tool_use' });
    await executeSandboxTurn({ client, data, turn: accepted, context });
    const requests: ChatModelRequest[] = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))
      .map(([, options]) => JSON.parse(options?.body as string).request);
    expect(requests).toHaveLength(2);
    expect(requests[0].messages.length).toBeLessThan(25);
    const initialHistory = requests[0].messages.length - 1;
    const finalHistory = requests[1].messages.length - 3;
    expect(finalHistory).toBeLessThan(initialHistory);
    for (const [round, request] of requests.entries()) {
      const retained = request.messages.length - 1 - round * 2;
      expect(retained).toBeGreaterThanOrEqual(16);
      expect(request.messages.slice(0, retained)).toEqual(context.messages.slice(-retained));
      expect(JSON.parse(request.messages[retained].content as string).message).toBe(accepted.message.content);
      expect(modelRequestSchema.safeParse(request).success).toBe(true);
      expect(Buffer.byteLength(canonicalJson(request))).toBeLessThanOrEqual(65_536);
      expect(Buffer.byteLength(canonicalJson({ ...request, messages: [context.messages[0], ...request.messages] })))
        .toBeGreaterThan(65_536);
    }
    expect(requests[1].messages.at(-1)?.content).toEqual([
      { type: 'tool_result', tool_use_id: 'read-large', content: toolText, is_error: false },
    ]);
    expect(context.messages).toHaveLength(24);
    expect(history.slice(0, 24)).toEqual(prior);
  });

  it('evicts an oversized old message without truncating the protected tail', async () => {
    history = [
      { ref: 'oversized', role: 'user', content: 'x'.repeat(32_001) },
      ...Array.from({ length: 16 }, (_, index) => ({ ref: `message-${index}`, role: 'assistant' as const, content: `m${index}` })),
      { ref: accepted.ref, role: 'user', content: accepted.message.content },
    ];
    const context = await data.prepare();
    expect(context.messages).toHaveLength(17);
    await executeSandboxTurn({ client, data, turn: accepted, context });
    const invocation = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/model/invoke'))!;
    const request: ChatModelRequest = JSON.parse(invocation[1]?.body as string).request;
    expect(request.messages.slice(0, -1)).toEqual(context.messages.slice(-16));
    expect(history[0].content).toHaveLength(32_001);
  });

  it.each(['x'.repeat(32_001), '界'.repeat(30_000)])('refuses unfit protected history without truncation or paid calls (%#)', async content => {
    history = [{ ref: 'protected', role: 'assistant', content },
      { ref: accepted.ref, role: 'user', content: accepted.message.content }];
    const context = await data.prepare();
    const tool = jest.spyOn(data, 'executeTool');
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toMatchObject({ code: 'incomplete' });
    expect(context.messages).toEqual([{ role: 'assistant', content }]);
    expect(tool).not.toHaveBeenCalled();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/model/invoke') || String(url).endsWith('/history/append'))).toBe(false);
    expect(history).toHaveLength(2);
  });

  it.each(['x'.repeat(32_000), '界'.repeat(30_000)])('refuses an unfit current message without truncating it (%#)', async content => {
    const largeTurn = { ...accepted, message: { ...accepted.message, content } };
    history = [{ ref: accepted.ref, role: 'user', content }];
    data = new SandboxDataRuntime(client, scope, largeTurn, workspace);
    const context = await data.prepare();
    await expect(executeSandboxTurn({ client, data, turn: largeTurn, context })).rejects.toMatchObject({ code: 'incomplete' });
    expect(context.userMessage).toBe(content);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/model/invoke') || String(url).endsWith('/history/append'))).toBe(false);
    expect(history).toEqual([{ ref: accepted.ref, role: 'user', content }]);
  });

  it.each(['x'.repeat(32_001), '界'.repeat(24_000)])('stops when a tool result cannot fit without issuing another paid call (%#)', async text => {
    const context = await data.prepare();
    const tool = jest.spyOn(data, 'executeTool').mockResolvedValue({ content: [{ type: 'text', text }] });
    modelResponses.push({ content: [
      { type: 'tool_use', id: 'read-unfit', name: 'read_memory', input: { id: memoryId } },
    ], stop_reason: 'tool_use' });
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toMatchObject({ code: 'incomplete' });
    expect(tool).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/history/append'))).toBe(false);
    expect(context.messages).toEqual([{ role: 'assistant', content: 'Previous owned reply' }]);
    expect(history).toHaveLength(2);
  });

  it.each([
    { status: 'running', handoff: 'prepared', reservation_status: 'reserved' },
    { status: 'unknown', handoff: 'unknown' },
    { reservation_status: 'reserved' },
    { usage: null },
    { usage: { input_tokens: 1, output_tokens: 1, estimated_usd: '-1' } },
    { stop_reason: 'max_tokens' },
    { session_id: 'other-session' },
  ])('does not run tools or record a reply from incomplete or foreign inference: %j', async changed => {
    modelResponses.push({ ...changed, content: [{ type: 'text', text: 'Unverified result' }] });
    const tool = jest.spyOn(data, 'executeTool');
    await expect(executeSandboxTurn({ client, data, turn: accepted, context: await data.prepare() })).rejects.toThrow();
    expect(tool).not.toHaveBeenCalled();
    expect(history).toHaveLength(2);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
  });

  it.each([
    ['Bash', { command: 'aws sts get-caller-identity' }],
    ['read_memory', { id: memoryId, user: 'victim' }],
    ['fetch_artifact', { id: artifactId, dest_path: '../escape' }],
  ])('rejects model-selected authority or file escape via %s and records only failure', async (name, input) => {
    modelResponses.push({ content: [{ type: 'tool_use', id: 'call-forged', name, input }], stop_reason: 'tool_use' });
    const context = await data.prepare();
    const before = fetchMock.mock.calls.length;
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toThrow();
    expect(fetchMock.mock.calls.slice(before).map(([url]) => new URL(String(url)).pathname)).toEqual([
      '/v1/chat/model/invoke', '/v1/chat/data/turn/result',
    ]);
    expect(JSON.parse(fetchMock.mock.calls.at(-1)![1]!.body as string)).toEqual({ ...scope, outcome: 'failed' });
    expect(history).toHaveLength(2);
  });

  it('does not replay a repeated tool identifier or write an incomplete reply', async () => {
    const call = { content: [{ type: 'tool_use', id: 'repeat', name: 'read_memory', input: { id: memoryId } }], stop_reason: 'tool_use' };
    modelResponses.push(call, call);
    const tool = jest.spyOn(data, 'executeTool');
    await expect(executeSandboxTurn({ client, data, turn: accepted, context: await data.prepare() })).rejects.toMatchObject({ code: 'invalid_response' });
    expect(tool).toHaveBeenCalledTimes(1);
    expect(history).toHaveLength(2);
  });

  it('refuses another tool round when its result cannot fit the bounded conversation', async () => {
    history = [...Array.from({ length: 32 }, (_, index) => ({ ref: `message-${index}`, role: 'user' as const, content: `m${index}` })),
      { ref: accepted.ref, role: 'user', content: accepted.message.content }];
    const context = await data.prepare();
    modelResponses.push(...Array.from({ length: 8 }, (_, round) => ({ content: [
      { type: 'tool_use', id: `read-${round}`, name: 'read_memory', input: { id: memoryId } },
    ], stop_reason: 'tool_use' })));
    const tool = jest.spyOn(data, 'executeTool');
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toMatchObject({ code: 'incomplete' });
    expect(tool).toHaveBeenCalledTimes(7);
    const calls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'));
    expect(calls).toHaveLength(8);
    expect(JSON.parse(calls.at(-1)?.[1]?.body as string).request.messages.slice(0, 16)).toEqual(context.messages.slice(-16));
    expect(history).toHaveLength(33);
  });

  it('stops before inference on local cancellation and propagates history-write failure', async () => {
    const context = await data.prepare();
    await expect(executeSandboxTurn({ client, data, turn: accepted, context }, AbortSignal.abort())).rejects.toThrow();
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/model/invoke'))).toBe(false);
    refusedPath = '/v1/chat/data/history/append';
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toThrow();
    expect(history).toHaveLength(2);
  });

  it('does not fall back or retry on model refusal', async () => {
    const context = await data.prepare();
    refusedPath = '/v1/chat/model/invoke';
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toThrow();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
    expect(history).toHaveLength(2);
  });

  it('does not report successful execution when history is saved but the result handoff is refused', async () => {
    const context = await data.prepare();
    refusedPath = '/v1/chat/data/turn/result';
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toMatchObject({ code: 'denied' });
    expect(history).toHaveLength(3);
    const results = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/turn/result'));
    expect(results.map(([, options]) => JSON.parse(options!.body as string).outcome)).toEqual(['completed', 'failed']);
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
  });

  it('only retries the same journal operation and records failure when both responses are lost', async () => {
    const context = await data.prepare();
    fetchMock.mockRejectedValueOnce(new Error('connection closed'));
    fetchMock.mockRejectedValueOnce(new Error('connection closed'));
    const before = fetchMock.mock.calls.length;
    await expect(executeSandboxTurn({ client, data, turn: accepted, context })).rejects.toMatchObject({ code: 'unavailable' });
    expect(fetchMock.mock.calls).toHaveLength(before + 3);
    expect(fetchMock.mock.calls[before][1]?.body).toBe(fetchMock.mock.calls[before + 1][1]?.body);
    expect(JSON.parse(fetchMock.mock.calls.at(-1)![1]!.body as string)).toEqual({ ...scope, outcome: 'failed' });
    expect(history).toHaveLength(2);
  });

  it('stops after cancellation during a tool without a subsequent inference or final reply', async () => {
    modelResponses.push({ content: [{ type: 'tool_use', id: 'read', name: 'read_memory', input: { id: memoryId } }], stop_reason: 'tool_use' });
    const context = await data.prepare();
    const execute = data.executeTool.bind(data);
    jest.spyOn(data, 'executeTool').mockImplementation(async (name, input) => {
      const result = await execute(name, input);
      cancellation.abort();
      return result;
    });
    await expect(executeSandboxTurn({ client, data, turn: accepted, context }, cancellation.signal)).rejects.toThrow();
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
    expect(history).toHaveLength(2);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/turn/result'))).toBe(false);
  });

  it('assembles only prior owned history and memory, and downloads protected attachments into the sandbox workspace', async () => {
    const prepared = await data.prepare();
    expect(prepared.messages).toEqual([{ role: 'assistant', content: 'Previous owned reply' }]);
    expect(prepared.userMessage).toBe(accepted.message.content);
    expect(prepared.memories[0].scope).toEqual({ user: 'owner', tenant: 'tenant' });
    expect(prepared.attachments).toEqual([{ id: artifactId, path: `attachments/${artifactId}` }]);
    expect(await readFile(path.join(workspace, prepared.attachments[0].path), 'utf8')).toBe('Attached owned content');
    const search = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/memory/search'))!;
    expect(JSON.parse(search[1]?.body as string)).not.toHaveProperty('user');
  });

  it('executes registered memory, history and artifact tools using bound scope', async () => {
    await expect(data.executeTool('save_fact', { content: 'An owned fact' })).resolves.toMatchObject({ content: [{ type: 'text' }] });
    const expanded = await data.executeTool('expand_summary', { summary_id: 'owned-summary' });
    expect(expanded.content[0].text).toContain('Previous owned reply');
    await writeFile(path.join(workspace, 'report.txt'), 'Owned result');
    const published = await data.executeTool('publish_artifact', { path: 'report.txt' });
    const reference = JSON.parse(published.content[0].text);
    expect(reference.url).toContain('/v1/chat/data/artifact/session-a/');
    expect(reference.url).not.toContain('signature=');
    expect(data.tools().map(tool => tool.name)).toEqual(expect.arrayContaining(['expand_summary', 'recall_memory', 'read_memory', 'fetch_artifact']));
    expect(data.tools().map(tool => tool.name)).not.toEqual(expect.arrayContaining(['Bash', 'get_credentials']));
  });

  it.each([
    ['recall_memory', { query: 'secret', user: 'victim' }],
    ['save_fact', { content: 'poison', tenant: 'other' }],
    ['expand_summary', { summary_id: 'guessed', session_id: 'other-session' }],
    ['fetch_artifact', { id: artifactId, dest_path: 'local.txt', run_id: 'other-run' }],
    ['Bash', { command: 'aws sts get-caller-identity' }],
  ] as const)('rejects forged scope or unknown tool %s before transport', async (name, input) => {
    await expect(data.executeTool(name, input)).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('validates inputs even when called through the SDK-compatible tool handler', async () => {
    const recall = data.tools().find(tool => tool.name === 'recall_memory')!;
    await expect(recall.handler({ query: 'secret', scope: { user: 'victim' } })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('does not reuse an earlier successful tool result after access is revoked', async () => {
    await data.executeTool('recall_memory', { query: 'preference' });
    refusedPath = '/v1/chat/data/memory/search';
    await expect(data.executeTool('recall_memory', { query: 'preference' })).rejects.toMatchObject({ code: 'denied' });
  });

  it.each([[404, 'denied'], [503, 'unavailable']] as const)('does not turn a %s data failure into empty context', async (status, code) => {
    refusedPath = '/v1/chat/data/history/read';
    failureStatus = status;
    await expect(data.prepare()).rejects.toMatchObject({ code });
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/artifact/session-a/'))).toBe(false);
  });

  it('refuses workspace escapes before downloading any bytes', async () => {
    await expect(data.executeTool('fetch_artifact', { id: artifactId, dest_path: '../escape.txt' }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('records only assistant output against the assembled history version and stable write key', async () => {
    await data.prepare();
    await data.record('The result');
    await data.record('The result');
    const writes = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/history/append'));
    expect(writes).toHaveLength(2);
    expect(writes[0][1]?.body).toBe(writes[1][1]?.body);
    expect(JSON.parse(writes[0][1]?.body as string)).toMatchObject({ ...scope, content: 'The result', expected_version: 2, user_turn_id: accepted.ref });
    expect(writes[0][1]?.body).not.toContain(accepted.message.content);
  });

  it('compacts large history through metered gateway summaries and preserves original source references', async () => {
    history = [
      { ref: 'prior-message', role: 'user', content: 'Historical record. '.repeat(4500) },
      ...Array.from({ length: 17 }, (_, index) => ({ ref: `message-${index}`, role: 'assistant' as const, content: `Reply ${index}` })),
      { ref: accepted.ref, role: 'user', content: accepted.message.content },
    ];
    await data.prepare();
    await data.record('The result');
    const modelCalls = fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'));
    expect(modelCalls.length).toBeGreaterThan(1);
    const compact = fetchMock.mock.calls.find(([url]) => String(url).endsWith('/history/compact'))!;
    expect(compact).toBeDefined();
    expect(JSON.parse(compact[1]?.body as string)).toMatchObject({
      ...scope, content: 'Owned history, preserving source references', source_ids: ['prior-message', 'message-0', 'message-1', 'message-2'],
    });
  });

  it('preserves recorded history when delegated summarization is refused', async () => {
    history = [
      { ref: 'prior-message', role: 'user', content: 'Historical record. '.repeat(4500) },
      ...Array.from({ length: 17 }, (_, index) => ({ ref: `message-${index}`, role: 'assistant' as const, content: `Reply ${index}` })),
      { ref: accepted.ref, role: 'user', content: accepted.message.content },
    ];
    await data.prepare();
    refusedPath = '/v1/chat/model/invoke';
    await expect(data.record('The result')).resolves.toBeUndefined();
    expect(history.some(item => item.ref === 'assistant-result' && item.content === 'The result')).toBe(true);
    expect(history[0].content).toBe('Historical record. '.repeat(4500));
    expect(fetchMock.mock.calls.filter(([url]) => String(url).endsWith('/model/invoke'))).toHaveLength(1);
    expect(fetchMock.mock.calls.some(([url]) => String(url).endsWith('/history/compact'))).toBe(false);
  });
});
