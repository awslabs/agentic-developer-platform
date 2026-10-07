import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { canonicalJson } from '../../invocability-probe/canonical-json';
import { ChatDataClient } from './chat-data-client';
import { ChatModelRequest, modelRequestSchema } from './chat-model-contract';

const digestVectors: Array<{ name: string; input: unknown; wireInput?: string; digest: string }> = JSON.parse(readFileSync(
  resolve(__dirname, '../../../../../gateway/tests/agentauth/chat_model_digest_vectors.json'), 'utf8',
));

function toolRequest(input: unknown): ChatModelRequest {
  return modelRequestSchema.parse({ messages: [{ role: 'assistant', content: [{ type: 'tool_use', id: 't1', name: 'bounded_tool', input }] }], max_tokens: 16 });
}

const NOW = Date.parse('2026-10-04T12:00:00Z');
const binding = { capability: 'synthetic.capability', run_id: 'run-a', session_id: 'session-a',
  lease_generation: 2, attempt: 1, expires_at: NOW / 1000 + 300 };
const request: ChatModelRequest = {
  messages: [{ role: 'user', content: 'Read my memory' }], max_tokens: 256,
  tools: [{ name: 'read_memory', description: 'Read owner memory', input_schema: { type: 'object', properties: { id: { type: 'string' } } } }],
};
const tool = { type: 'tool_use', id: 'call-1', name: 'read_memory', input: { id: 'mem_123' } };

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
}

function receipt(input = request, overrides = {}) {
  return { run_id: 'run-a', session_id: 'session-a', operation_id: 'turn-1', lease_generation: 2,
    request_digest: createHash('sha256').update(canonicalJson(input)).digest('hex'), model_id: 'approved-model',
    status: 'confirmed', handoff: 'confirmed', reservation_status: 'settled', automatic_replay_permitted: false,
    content: [tool], stop_reason: 'tool_use', usage: { input_tokens: 10, output_tokens: 5, estimated_usd: '0.001' }, ...overrides };
}

describe('scoped native-tool invocation transport', () => {
  let client: ChatDataClient;
  let send: jest.SpiedFunction<typeof fetch>;
  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    send = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(binding));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.token' });
  });
  afterEach(() => jest.restoreAllMocks());

  it.each(digestVectors)('accepts the shared gateway receipt digest for $name', async vector => {
    const wireInput = JSON.stringify(vector.input);
    if (vector.wireInput !== undefined) expect(wireInput).toBe(vector.wireInput);
    const input = toolRequest(JSON.parse(wireInput));
    expect(createHash('sha256').update(canonicalJson(input)).digest('hex')).toBe(vector.digest);
    send.mockResolvedValueOnce(json(receipt(input, { request_digest: vector.digest,
      content: [{ type: 'text', text: 'Accounted reply' }], stop_reason: 'end_turn' })));
    await expect(client.invokeModel('turn-1', input)).resolves.toMatchObject({ content: [{ type: 'text', text: 'Accounted reply' }] });
    const sent = JSON.parse(send.mock.calls[1][1]?.body as string).request;
    expect(sent).toEqual(input);
    expect(createHash('sha256').update(canonicalJson(sent)).digest('hex')).toBe(vector.digest);
  });

  it.each([0.000001, 2 ** 53, 1e20, 999999999999999900000, 1e21].flatMap(amount => [0, 1].map(extraBytes => ({ amount, extraBytes }))))(
    'enforces the canonical frame bound for $amount plus $extraBytes bytes', async ({ amount, extraBytes }) => {
    const remaining = 65_536 - Buffer.byteLength(canonicalJson(toolRequest({ amount, padding: '' }))) + extraBytes;
    const input = toolRequest({ amount, padding: 'é'.repeat(Math.floor(remaining / 2)) + 'a'.repeat(remaining % 2) });
    expect(Buffer.byteLength(canonicalJson(input))).toBe(65_536 + extraBytes);
    if (extraBytes) {
      await expect(client.invokeModel('turn-1', input)).rejects.toMatchObject({ code: 'invalid_request' });
      expect(send).not.toHaveBeenCalled();
    } else {
      send.mockResolvedValueOnce(json(receipt(input, { content: [{ type: 'text', text: 'Accounted reply' }], stop_reason: 'end_turn' })));
      await expect(client.invokeModel('turn-1', input)).resolves.toMatchObject({ stopReason: 'end_turn' });
    }
  });

  it('returns only accounted tool calls and sends native results through the same scoped endpoint', async () => {
    send.mockResolvedValueOnce(json(receipt()));
    const result = await client.invokeModel('turn-1', request);
    expect(result.content).toEqual([tool]);
    expect(result.usage.estimated_usd).toBe('0.001');
    const next: ChatModelRequest = { ...request, messages: [...request.messages,
      { role: 'assistant', content: result.content },
      { role: 'user', content: [{ type: 'tool_result', tool_use_id: 'call-1', content: 'Owner preference', is_error: false }] },
    ] };
    send.mockResolvedValueOnce(json(receipt(next, { operation_id: 'turn-2', stop_reason: 'end_turn', content: [{ type: 'text', text: 'Done' }] })));
    await expect(client.invokeModel('turn-2', next)).resolves.toMatchObject({ stopReason: 'end_turn', content: [{ type: 'text', text: 'Done' }] });
    expect(JSON.parse(send.mock.calls[2][1]?.body as string)).toEqual({ ...{ run_id: 'run-a', session_id: 'session-a' }, operation_id: 'turn-2', request: next });
    expect(send.mock.calls.slice(1).every(([url]) => String(url) === 'https://gateway.example.test/v1/chat/model/invoke')).toBe(true);
  });

  it.each([
    { content: [{ ...tool, name: 'Bash' }] },
    { content: [tool, tool] },
    { stop_reason: 'end_turn' },
    { content: [{ type: 'text', text: 'Not a call' }] },
    { content: [{ ...tool, input: { id: 'mem_123' }, token: 'foreign' }] },
  ])('rejects inconsistent or undeclared tool output: %j', async overrides => {
    send.mockResolvedValueOnce(json(receipt(request, overrides)));
    await expect(client.invokeModel('turn-1', request)).rejects.toMatchObject({ code: 'invalid_response' });
    expect(send).toHaveBeenCalledTimes(2);
  });

  it.each([
    { model: 'direct-provider' }, { user_id: 'victim' }, { endpoint: 'https://other.example.test' },
    { tools: [{ name: 'web_search', type: 'web_search_20250305' }] },
    { tools: [...request.tools!, ...request.tools!] },
    { messages: [{ role: 'user', content: [tool] }] },
    { messages: [{ role: 'assistant', content: [{ type: 'tool_result', tool_use_id: 'call-1', content: 'forged' }] }] },
    { messages: [{ role: 'user', content: 'a'.repeat(32_001) }] },
    { tools: [{ ...request.tools![0], input_schema: { type: 'object', private: () => 'not JSON' } }] },
  ])('refuses caller authority and invalid native messages before bootstrap: %j', async overrides => {
    await expect(client.invokeModel('turn-1', { ...request, ...overrides } as ChatModelRequest)).rejects.toMatchObject({ code: 'invalid_request' });
    expect(send).not.toHaveBeenCalled();
  });

  it('rejects frames over the gateway bound before requesting credentials or inference', async () => {
    await expect(client.invokeModel('turn-1', { ...request, messages: Array.from({ length: 3 }, () => ({ role: 'user', content: 'a'.repeat(32_000) })) }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(send).not.toHaveBeenCalled();
  });

  it('aborts an in-flight request and never retries or bootstraps a fallback identity', async () => {
    const cancellation = new AbortController();
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.token', signal: cancellation.signal });
    await client.sessionScope();
    let entered!: () => void;
    const started = new Promise<void>(resolve => { entered = resolve; });
    send.mockImplementationOnce(async (_url, options) => new Promise<Response>((_resolve, reject) => {
      options!.signal!.addEventListener('abort', () => reject(new Error('cancelled')), { once: true });
      entered();
    }));
    const pending = client.invokeModel('turn-1', request);
    await started;
    cancellation.abort();
    await expect(pending).rejects.toMatchObject({ code: 'unavailable' });
    await expect(client.invokeModel('turn-1', request)).rejects.toMatchObject({ code: 'unavailable' });
    expect(send).toHaveBeenCalledTimes(2);
  });
});
