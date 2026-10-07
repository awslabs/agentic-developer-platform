import { ChatDataClient, TextModelRequest } from './chat-data-client';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const binding = {
  capability: 'synthetic.capability', run_id: 'run-a', session_id: 'session-a',
  lease_generation: 2, attempt: 1, expires_at: NOW / 1000 + 300,
};
const request: TextModelRequest = {
  system: 'Summarize this', messages: [{ role: 'user', content: 'Résumé 😀' }], max_tokens: 16,
};
const receipt = {
  run_id: 'run-a', session_id: 'session-a', operation_id: 'summary-1', lease_generation: 2,
  request_digest: '58da3f29cb5fd470a97882e6516c92e9489d3d3645aeb917b98f40002d9e56cb', model_id: 'approved-model',
  status: 'confirmed', handoff: 'confirmed', reservation_status: 'settled', automatic_replay_permitted: false,
  content: [{ type: 'text', text: 'A summary' }], stop_reason: 'end_turn',
  usage: { input_tokens: 5, output_tokens: 3, estimated_usd: '0.001' },
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('scoped text-model invocation transport', () => {
  let client: ChatDataClient;
  let fetchMock: jest.SpiedFunction<typeof fetch>;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(binding));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.workload.token' });
  });
  afterEach(() => jest.restoreAllMocks());

  it('binds text to the assigned run and validates the Python-compatible Unicode request digest and usage receipt', async () => {
    fetchMock.mockResolvedValueOnce(json(receipt));
    await expect(client.invokeTextModel('summary-1', request)).resolves.toEqual({
      text: 'A summary', modelId: 'approved-model', stopReason: 'end_turn', usage: receipt.usage,
    });
    const [url, options] = fetchMock.mock.calls[1];
    expect(url).toBe('https://gateway.example.test/v1/chat/model/invoke');
    expect(JSON.parse(options?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', operation_id: 'summary-1', request,
    });
    expect(options).toMatchObject({ redirect: 'error', headers: {
      'Content-Type': 'application/json', Authorization: 'Bearer synthetic.capability', 'X-Adp-Workload-Token': 'sandbox.workload.token',
    } });
  });

  it('retries a lost reply with the same operation and captured request, never a fresh paid call', async () => {
    const mutable = JSON.parse(JSON.stringify(request)) as TextModelRequest;
    fetchMock.mockImplementationOnce(async () => {
      mutable.messages[0].content = 'changed after dispatch';
      throw new Error('lost reply');
    }).mockResolvedValueOnce(json(receipt));
    await expect(client.invokeTextModel('summary-1', mutable)).resolves.toMatchObject({ text: 'A summary' });
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
  });

  it('opts owner output into trusted delivery without accepting a recipient', async () => {
    fetchMock.mockResolvedValueOnce(json(receipt));
    await expect(client.invokeModel('summary-1', request, undefined, true)).resolves.toMatchObject({
      content: receipt.content, modelId: 'approved-model',
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', operation_id: 'summary-1', request, deliver_response: true,
    });
  });

  it.each([
    { run_id: 'other-run' }, { session_id: 'other-session' }, { operation_id: 'other-operation' },
    { lease_generation: 3 }, { request_digest: 'a'.repeat(64) },
  ])('rejects a receipt with substituted scope or payload: %j', async changed => {
    fetchMock.mockResolvedValueOnce(json({ ...receipt, ...changed }));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    { status: 'running', handoff: 'prepared', reservation_status: 'reserved', usage: null, content: undefined, stop_reason: undefined },
    { status: 'unknown', handoff: 'unknown', reservation_status: 'unknown', usage: null, content: undefined, stop_reason: undefined },
    { reservation_status: 'reserved' },
  ])('does not treat pending or unaccounted inference as a usable summary', async changed => {
    fetchMock.mockResolvedValueOnce(json({ ...receipt, ...changed }));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'incomplete' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each([
    { usage: null }, { usage: { input_tokens: 1, output_tokens: 17, estimated_usd: '0.001' } },
    { usage: { input_tokens: 1, output_tokens: 1, estimated_usd: '-1' } },
    { automatic_replay_permitted: true }, { content: [{ type: 'tool_use', name: 'Bash', input: {} }] },
  ])('rejects unusable accounting or non-text output', async changed => {
    fetchMock.mockResolvedValueOnce(json({ ...receipt, ...changed }));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it.each([{ model: 'other-model' }, { run_id: 'other-run' }, { tools: [] }, { max_tokens: 0 }])(
    'refuses caller authority or unsupported request fields before bootstrap', async changed => {
      await expect(client.invokeTextModel('summary-1', { ...request, ...changed })).rejects.toMatchObject({ code: 'invalid_request' });
      expect(fetchMock).not.toHaveBeenCalled();
    },
  );

  it('propagates refusal without a direct-provider fallback', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: { error: 'chat_scope_refused' } }, 404));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'denied' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
