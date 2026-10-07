import { ChatDataClient } from './chat-data-client';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const binding = { run_id: 'run-a', session_id: 'session-a', attempt: 1, lease_generation: 2,
  capability: 'synthetic.scoped.capability', expires_at: NOW / 1000 + 300 };
const result = { outcome: 'completed' as const, message_id: 'msg-reply' };
const receipt = { ...result, run_id: binding.run_id, session_id: binding.session_id, attempt: binding.attempt,
  lease_generation: binding.lease_generation, sandbox_uid: 'sandbox-a', terminal: false };

function json(value: unknown, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('sandbox durable result handoff', () => {
  let client: ChatDataClient;
  let fetchMock: jest.SpiedFunction<typeof fetch>;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token',
      sleep: async () => undefined });
    fetchMock = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(binding)).mockResolvedValue(json(receipt));
  });
  afterEach(() => jest.restoreAllMocks());

  it('submits an existing reply under the gateway-issued run and lease, without terminal authority', async () => {
    await client.submitTurnResult(result);
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe('https://gateway.example.test/v1/chat/data/turn/result');
    expect(JSON.parse(options!.body as string)).toEqual({ ...result, run_id: 'run-a', session_id: 'session-a' });
    expect(options!.headers).toMatchObject({ Authorization: `Bearer ${binding.capability}`, 'X-Adp-Workload-Token': 'workload.token' });
  });

  it('reports failure without exception text or a fabricated assistant reference', async () => {
    fetchMock.mockResolvedValue(json({ ...receipt, outcome: 'failed', message_id: null }));
    await client.submitTurnResult({ outcome: 'failed' });
    expect(JSON.parse(fetchMock.mock.calls[1][1]!.body as string)).toEqual({ outcome: 'failed', run_id: 'run-a', session_id: 'session-a' });
  });

  it.each([
    { run_id: 'other' }, { session_id: 'other' }, { attempt: 2 }, { lease_generation: 3 }, { terminal: true },
    { outcome: 'failed' }, { message_id: 'other' }, { message_id: null }, { unexpected: 'value' },
  ])('rejects mismatched or terminal receipts: %j', async changes => {
    fetchMock.mockResolvedValue(json({ ...receipt, ...changes }));
    await expect(client.submitTurnResult(result)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it.each([
    { outcome: 'cancelled' }, { outcome: 'completed' }, { outcome: 'failed', message_id: 'forged' },
    { ...result, run_id: 'other' }, { ...result, terminal: true },
  ])('rejects caller-supplied authority or malformed outcome before transport: %j', async input => {
    await expect(client.submitTurnResult(input as typeof result)).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([404, 409, 503])('propagates refused or uncertain handoff without credential fallback: %i', async status => {
    fetchMock.mockResolvedValue(json({ detail: { error: 'chat_result_unavailable' } }, status));
    await expect(client.submitTurnResult(result)).rejects.toBeInstanceOf(Error);
    expect(fetchMock.mock.calls.every(([url]) => String(url).startsWith('https://gateway.example.test/v1/chat/data/'))).toBe(true);
  });
});
