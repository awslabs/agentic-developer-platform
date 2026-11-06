import { randomBytes } from 'node:crypto';
import { unlink, readFile } from 'node:fs/promises';
import { query, createSdkMcpServer, tool } from '@anthropic-ai/claude-agent-sdk';
import { executeCrewTurn } from './crew-turn';
import { ChatDataClient, ChatDataError } from './gateway/chat-data-client';
import { withSessionHeartbeat } from './sandbox-session-heartbeat';

jest.mock('@anthropic-ai/claude-agent-sdk', () => ({
  query: jest.fn(), createSdkMcpServer: jest.fn(({ tools }) => ({ tools })),
  tool: jest.fn((name, _description, _schema, handler) => ({ name, handler })),
}));

const modelId = 'gateway-selected-model';
const filename = `/tmp/workspace/crew-${randomBytes(8).toString('hex')}.txt`;
const result = { content: [{ type: 'text', text: 'Done' }], stopReason: 'end_turn', modelId,
  usage: { input_tokens: 4, output_tokens: 2 } };
const invokeModel = jest.fn();
const publishToolCall = jest.fn();
const record = jest.fn();
const client = { sessionMode: jest.fn(async () => 'persistent'), withSignal: jest.fn(),
  crewRunId: '00000000-0000-4000-8000-000000000001',
  requestTools: jest.fn(async () => ({ status: 200, body: { tools: [{ name: 'websearch___search' }] } })),
  invokeModel, publishToolCall, submitTurnResult: jest.fn(async () => undefined),
  renewSession: jest.fn(async () => ({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' })),
  sessionState: jest.fn(async () => ({ mode: 'persistent', health: 'active' })) };
client.withSignal.mockImplementation(() => client);
const prepared = { client, model: { modelId }, data: { tools: () => [], record },
  context: { userMessage: `create ${filename} with hello`, messages: [], memories: [], attachments: [] } };

afterEach(async () => {
  await unlink(filename).catch(() => undefined);
  jest.clearAllMocks();
  jest.restoreAllMocks();
});

test('SDK Messages use the gateway and a scoped workspace tool creates a file', async () => {
  invokeModel.mockResolvedValueOnce({ content: [{ type: 'tool_use', id: 'write-1', name: 'write_file',
    input: { file_path: filename, content: 'hello' } }], stopReason: 'tool_use', modelId,
  usage: { input_tokens: 5, output_tokens: 3 } }).mockResolvedValueOnce(result);
  (query as jest.Mock).mockImplementation(({ options }) => (async function* () {
    expect(options.mcpServers['adp-tools'].env.ADP_CREW_TOOLS_URL).toBe(options.env.ANTHROPIC_BASE_URL);
    expect(options.allowedTools).toContain('mcp__adp-tools__websearch___search');
    expect((await options.canUseTool('mcp__adp-tools__websearch___search')).behavior).toBe('allow');
    expect((await options.canUseTool('mcp__adp-tools__browser___start')).behavior).toBe('deny');
    const headers = { 'x-api-key': options.env.ANTHROPIC_API_KEY, 'content-type': 'application/json' };
    const first = await fetch(`${options.env.ANTHROPIC_BASE_URL}/v1/messages`, { method: 'POST', headers,
      body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'Create the file' }], max_tokens: 128, stream: true,
        tools: [{ name: 'write_file', description: 'Write a file', input_schema: { type: 'object' } }] }) });
    expect(first.status).toBe(200);
    expect(await first.text()).toContain('event: content_block_start');
    const workspaceTool = options.mcpServers.workspace.tools.find((entry: { name: string }) => entry.name === 'write_file');
    expect(await workspaceTool.handler({ file_path: filename, content: 'hello' })).toMatchObject({
      content: [{ text: `Wrote ${filename}` }],
    });
    const second = await fetch(`${options.env.ANTHROPIC_BASE_URL}/v1/messages`, { method: 'POST', headers,
      body: JSON.stringify({ model: modelId, messages: [{ role: 'user', content: 'Create the file' },
        { role: 'assistant', content: [{ type: 'tool_use', id: 'write-1', name: 'write_file', input: { file_path: filename, content: 'hello' } }] },
        { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'write-1', content: 'Wrote file' }] }],
      max_tokens: 128 }) });
    expect(second.status).toBe(200);
    yield { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'write-1', name: 'write_file',
      input: { file_path: filename, content: 'hello' } }] } };
    yield { type: 'result', subtype: 'success', is_error: false, result: 'Done' };
  })());
  await executeCrewTurn(prepared as never);
  expect(await readFile(filename, 'utf8')).toBe('hello');
  expect(invokeModel).toHaveBeenCalledTimes(2);
  expect(invokeModel.mock.calls.every(call => call[3] === true)).toBe(true);
  expect(publishToolCall).toHaveBeenCalledWith('write-1', 'write_file', { file_path: filename, content: 'hello' });
  expect(record).toHaveBeenCalledWith('Done');
});

test('unavailable tool discovery fails the Crew turn instead of silently omitting tools', async () => {
  client.requestTools.mockResolvedValueOnce({ status: 503, body: { tools: [] } });
  await expect(executeCrewTurn(prepared as never)).rejects.toThrow(ChatDataError);
  expect(query).not.toHaveBeenCalled();
  expect(client.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed' });
  expect(record).not.toHaveBeenCalled();
});

test.each(['x'.repeat(20_000), 'x'.repeat(32_000), '界'.repeat(10_667), '😀'.repeat(8_000)])(
  'large workspace writes complete through the real event client %#', async content => {
    const args = { file_path: filename, content };
    const published: Array<{ args: unknown }> = [];
    jest.spyOn(globalThis, 'fetch').mockImplementation(async (url, options) => {
      if (String(url).endsWith('/bootstrap')) return new Response(JSON.stringify({
        capability: 'synthetic.capability', run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent',
        lease_generation: 2, attempt: 1, expires_at: Math.floor(Date.now() / 1000) + 300,
      }), { headers: { 'content-type': 'application/json' } });
      expect(String(url)).toBe('https://gateway.example.test/v1/chat/model/tool-events');
      const event = JSON.parse(options?.body as string);
      expect(Buffer.byteLength(JSON.stringify(event.args))).toBeLessThanOrEqual(16_000);
      published.push(event);
      return new Response(JSON.stringify({ accepted: true }), { headers: { 'content-type': 'application/json' } });
    });
    const eventClient = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'synthetic.workload', crewRunId: '00000000-0000-4000-8000-000000000001' });
    (query as jest.Mock).mockImplementation(({ options }) => (async function* () {
      yield { type: 'assistant', message: { content: [{ type: 'tool_use', id: 'large-write',
        name: 'mcp__workspace__write_file', input: args }] } };
      const workspaceTool = options.mcpServers.workspace.tools.find((entry: { name: string }) => entry.name === 'write_file');
      expect(await workspaceTool.handler(args)).not.toHaveProperty('isError');
      yield { type: 'result', subtype: 'success', is_error: false, result: 'Wrote the large file' };
    })());
    await executeCrewTurn({ ...prepared, client: { ...client,
      publishToolCall: eventClient.publishToolCall.bind(eventClient) } } as never);
    expect(await readFile(filename, 'utf8')).toBe(content);
    expect(published).toHaveLength(1);
    expect(published[0].args).toEqual({ _adp_tool_event: {
      arguments_omitted: true, utf8_bytes: Buffer.byteLength(JSON.stringify(args)), limit_bytes: 16_000,
    } });
    expect(record).toHaveBeenCalledWith('Wrote the large file');
    expect(client.submitTurnResult).not.toHaveBeenCalled();
  },
);

test('workspace tool reports unsafe writes without aborting the SDK turn', async () => {
  (query as jest.Mock).mockImplementation(({ options }) => (async function* () {
    expect(options.tools).toEqual([]);
    expect(options.permissionMode).toBe('default');
    expect(options.thinking).toEqual({ type: 'disabled' });
    expect(await options.canUseTool('Bash', {})).toMatchObject({ behavior: 'deny' });
    const workspaceTool = options.mcpServers.workspace.tools.find((entry: { name: string }) => entry.name === 'write_file');
    expect(await workspaceTool.handler({ file_path: '/app/no-write.txt', content: 'unsafe' })).toMatchObject({
      isError: true, content: [{ text: expect.stringContaining('Path must be inside /tmp/workspace') }],
    });
    yield { type: 'result', subtype: 'success', is_error: false, result: 'I cannot write outside the workspace.' };
  })());
  await executeCrewTurn(prepared as never);
  expect(record).toHaveBeenCalledWith('I cannot write outside the workspace.');
});

test('lease fencing cancels the SDK process and a subsequent turn runs', async () => {
  client.renewSession.mockResolvedValueOnce({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' })
    .mockRejectedValueOnce(new Error('fenced'));
  (query as jest.Mock).mockImplementation(({ options }) => (async function* () {
    await new Promise<void>((_resolve, reject) => options.abortController.signal.addEventListener('abort',
      () => reject(new Error('SDK stopped')), { once: true }));
  })());
  await expect(withSessionHeartbeat(client as never, signal => executeCrewTurn(prepared as never, signal), undefined, 10))
    .rejects.toThrow('fenced');
  expect(client.submitTurnResult).not.toHaveBeenCalled();
  client.renewSession.mockResolvedValue({ run_id: 'run-a', session_id: 'session-a', session_mode: 'persistent' });
  (query as jest.Mock).mockImplementation(() => (async function* () {
    yield { type: 'result', subtype: 'success', is_error: false, result: 'Recovered' };
  })());
  await withSessionHeartbeat(client as never, signal => executeCrewTurn(prepared as never, signal), undefined, 10);
  expect(record).toHaveBeenCalledWith('Recovered');
});

test('SDK model failures submit failed and retain the gateway error class', async () => {
  invokeModel.mockRejectedValueOnce(new ChatDataError('denied'));
  (query as jest.Mock).mockImplementation(({ options }) => (async function* () {
    const response = await fetch(`${options.env.ANTHROPIC_BASE_URL}/v1/messages`, { method: 'POST',
      headers: { 'x-api-key': options.env.ANTHROPIC_API_KEY, 'content-type': 'application/json' },
      body: JSON.stringify({ model: modelId, max_tokens: 128, messages: [{ role: 'user', content: 'Hello' }] }) });
    expect(response.status).toBe(403);
    throw new Error('SDK API error');
  })());
  await expect(executeCrewTurn(prepared as never)).rejects.toMatchObject({ code: 'denied' });
  expect(client.submitTurnResult).toHaveBeenCalledWith({ outcome: 'failed' });
});
