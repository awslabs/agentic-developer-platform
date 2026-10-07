import { createHash } from 'node:crypto';
import { ChatDataClient } from './gateway/chat-data-client';
import { SandboxDataRuntime } from './sandbox-data';
import { startSandboxTurn } from './sandbox-entrypoint';

jest.mock('../lib/projectedWorkloadToken', () => ({ readIdentityToken: jest.fn(() => 'sandbox.workload.token') }));

const env: NodeJS.ProcessEnv = {
  ADP_CHAT_DATA_ENABLED: 'true', ADP_CHAT_MODEL_POLICY_ENABLED: 'true',
  ADP_CHAT_DATA_URL: 'https://gateway.example.test', CONTEXT_STRATEGY: 'gateway',
  MEMORY_STRATEGY: 'gateway', ARTIFACT_STRATEGY: 'gateway', ADP_WORKLOAD_TOKEN_FILE: '/var/run/adp-model/token',
};

afterEach(() => jest.restoreAllMocks());

test('interleaves five turns in two exclusive runtimes retaining only their own first-turn tool context', async () => {
  type State = { session: string; sequence: number; requests: number; canary?: string; admitted: number[] };
  const sessions = new Map<ChatDataClient, State>();
  const state = (client: ChatDataClient) => {
    if (!sessions.has(client)) sessions.set(client, { session: `session-${sessions.size + 1}`, sequence: 1, requests: 0, admitted: [] });
    return sessions.get(client)!;
  };
  const scope = (client: ChatDataClient) => ({ session_id: state(client).session, run_id: `${state(client).session}-turn-${state(client).sequence}` });
  jest.spyOn(ChatDataClient.prototype, 'sessionScope').mockImplementation(async function (this: ChatDataClient) { return scope(this); });
  jest.spyOn(ChatDataClient.prototype, 'sessionMode').mockResolvedValue('persistent');
  jest.spyOn(ChatDataClient.prototype, 'renewSession').mockImplementation(async function (this: ChatDataClient) { return { ...scope(this), session_mode: 'persistent' }; });
  jest.spyOn(ChatDataClient.prototype, 'nextTurn').mockImplementation(async function (this: ChatDataClient) {
    return { ref: `user_${createHash('sha256').update(scope(this).run_id).digest('hex')}`, session_sequence: state(this).sequence,
      message: { role: 'user', content: `Message ${state(this).sequence}`, ts: '2026-10-04T12:00:00Z', tokens: 3, parts: [] } };
  });
  jest.spyOn(ChatDataClient.prototype, 'modelDecision').mockImplementation(async function (this: ChatDataClient) {
    return { modelId: 'approved-model', runId: scope(this).run_id, tenantId: 'tenant-a', generation: state(this).sequence };
  });
  const prepare = jest.spyOn(SandboxDataRuntime.prototype, 'prepare').mockResolvedValue({
    messages: [], protectedMessageCount: 0, memories: [], attachments: [], userMessage: 'Current user message',
    meta: { rawMessageCount: 0, summaryCount: 0, estimatedTokens: 0, compactionTriggered: false },
  });
  const record = jest.spyOn(SandboxDataRuntime.prototype, 'record').mockResolvedValue();
  const tools = jest.spyOn(SandboxDataRuntime.prototype, 'executeTool').mockImplementation(async () => ({
    content: [{ type: 'text', text: `tool-canary-${tools.mock.calls.length}` }],
  }));
  jest.spyOn(ChatDataClient.prototype, 'invokeModel').mockImplementation(async function (this: ChatDataClient, _id, request) {
    const current = state(this);
    current.requests++;
    const base = { modelId: 'approved-model', usage: { input_tokens: 3, output_tokens: 2, estimated_usd: '0.01' } };
    if (current.requests === 1) return { ...base, stopReason: 'tool_use',
      content: [{ type: 'tool_use', id: `read-${current.session}`, name: 'history_read', input: {} }] };
    if (current.sequence === 1) {
      const result = request.messages.at(-1)!.content as Array<{ content: string }>;
      current.canary = result[0].content;
    }
    expect(JSON.stringify(request.messages)).toContain(current.canary);
    for (const other of sessions.values()) {
      if (other !== current && other.canary) expect(JSON.stringify(request.messages)).not.toContain(other.canary);
    }
    return { ...base, stopReason: 'end_turn', content: [{ type: 'text', text: `Reply ${current.sequence}` }] };
  });
  jest.spyOn(ChatDataClient.prototype, 'nextMailboxTurn').mockImplementation(async function (this: ChatDataClient, after) {
    const current = state(this);
    expect(after).toBe(current.sequence);
    if (after === 5) throw new Error('fixture session ended');
    return { sequence: after + 1, turn_id: `${current.session}-turn-${after + 1}`, message: `Message ${after + 1}` };
  });
  jest.spyOn(ChatDataClient.prototype, 'admitMailboxTurn').mockImplementation(async function (this: ChatDataClient, turn) {
    const current = state(this);
    expect(turn.sequence).toBe(current.sequence + 1);
    expect(turn.turn_id).toBe(`${current.session}-turn-${turn.sequence}`);
    current.sequence = turn.sequence;
    current.admitted.push(turn.sequence);
  });
  const outcomes = await Promise.allSettled([startSandboxTurn(env), startSandboxTurn(env)]);
  expect(outcomes).toEqual([
    { status: 'rejected', reason: new Error('fixture session ended') },
    { status: 'rejected', reason: new Error('fixture session ended') },
  ]);
  expect(sessions.size).toBe(2);
  for (const current of sessions.values()) {
    expect(current.admitted).toEqual([2, 3, 4, 5]);
    expect(current.requests).toBe(6);
  }
  expect(tools).toHaveBeenCalledTimes(2);
  expect(record).toHaveBeenCalledTimes(10);
  expect(prepare).toHaveBeenCalledTimes(10);
});
