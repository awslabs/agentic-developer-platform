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

  it('routes persistent inference with only the minted Crew run header', async () => {
    const crewRunId = '00000000-0000-4000-8000-000000000001';
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json(receipt));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId });
    await expect(client.invokeTextModel('summary-1', request)).resolves.toMatchObject({ text: 'A summary' });
    expect(fetchMock.mock.calls[0][1]?.headers).not.toHaveProperty('x-agent-runid');
    expect(fetchMock.mock.calls[1][0]).toBe('https://gateway.example.test/v1/chat/model/invoke');
    expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({ 'x-agent-runid': crewRunId });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', operation_id: 'summary-1', request,
    });
  });

  it('preserves the pinned Crew identity in cancellation-scoped clients', async () => {
    const crewRunId = '00000000-0000-4000-8000-000000000001';
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json(receipt));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId });
    await client.sessionScope();
    const scoped = client.withSignal(new AbortController().signal);
    await expect(scoped.invokeTextModel('summary-1', request)).resolves.toMatchObject({ text: 'A summary' });
    expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({ 'x-agent-runid': crewRunId });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('posts tool events only through a persistent run-bound gateway capability', async () => {
    const crewRunId = '00000000-0000-4000-8000-000000000001';
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockResolvedValueOnce(json({ accepted: true }));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId });
    await client.publishToolCall('call-1', 'write_file', { file_path: '/tmp/workspace/a.txt' });
    expect(fetchMock.mock.calls[1][0]).toBe('https://gateway.example.test/v1/chat/model/tool-events');
    expect(fetchMock.mock.calls[1][1]?.headers).toMatchObject({
      Authorization: 'Bearer synthetic.capability', 'x-agent-runid': crewRunId,
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', operation_id: 'tool_call-1',
      tool_call_id: 'call-1', name: 'write_file', args: { file_path: '/tmp/workspace/a.txt' },
    });
    fetchMock.mockReset().mockResolvedValueOnce(json(binding));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.workload.token' });
    await expect(client.publishToolCall('call-1', 'write_file', {})).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(['x', '界', '😀'])('bounds %s tool arguments by UTF-8 bytes without failing valid calls', async character => {
    const crewRunId = '00000000-0000-4000-8000-000000000001';
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockImplementation(async () => json({ accepted: true }));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId });
    const empty = { file_path: '/tmp/workspace/a.txt', content: '' };
    const remaining = 16_000 - Buffer.byteLength(JSON.stringify(empty));
    const content = character.repeat(Math.floor(remaining / Buffer.byteLength(character))) +
      'x'.repeat(remaining % Buffer.byteLength(character));
    for (const size of [15_999, 16_000, 16_001, 20_049]) {
      const args = { ...empty, content: size < 16_000 ? 'x'.repeat(remaining - 1) : content + 'x'.repeat(size - 16_000) };
      expect(Buffer.byteLength(JSON.stringify(args))).toBe(size);
      await expect(client.publishToolCall(`call-${size}`, 'write_file', args)).resolves.toBeUndefined();
      const published = JSON.parse(fetchMock.mock.calls.at(-1)![1]!.body as string);
      expect(published.args).toEqual(size <= 16_000 ? args : {
        _adp_tool_event: { arguments_omitted: true, utf8_bytes: size, limit_bytes: 16_000 },
      });
      expect(Buffer.byteLength(JSON.stringify(published.args))).toBeLessThanOrEqual(16_000);
    }
  });

  it('refuses a missing Crew run binding before inference', async () => {
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('refuses a Crew run header on an ephemeral invocation', async () => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId: '00000000-0000-4000-8000-000000000001' });
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it('rejects a forged or revoked Crew run at the gateway without using another model route', async () => {
    const crewRunId = '00000000-0000-4000-8000-000000000002';
    fetchMock.mockReset().mockResolvedValueOnce(json({ ...binding, session_mode: 'persistent' }))
      .mockImplementationOnce(async (_url, options) => {
        expect(options?.headers).toMatchObject({ 'x-agent-runid': crewRunId });
        return json({ detail: { error: 'chat_scope_refused' } }, 403);
      });
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId });
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'denied', status: 403 });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('rejects invalid Crew run headers before bootstrap', () => {
    expect(() => new ChatDataClient({ baseUrl: 'https://gateway.example.test',
      workloadToken: async () => 'sandbox.workload.token', crewRunId: 'forged\r\nx-agent-runid: stolen' }))
      .toThrow('Chat data invalid_request');
    expect(fetchMock).not.toHaveBeenCalled();
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

  it.each([
    ['rejected', 'not_started', 'not_reserved', 'model_admission_refused', 'denied'],
    ['unknown', 'unknown', 'unknown', 'model_outcome_unknown', 'incomplete'],
  ])('carries the %s receipt\'s error code, class and bounded detail on the failure (#8048)', async (status, handoff, reservation, code, failure) => {
    fetchMock.mockResolvedValueOnce(json({ ...receipt, status, handoff, reservation_status: reservation, usage: null,
      content: undefined, stop_reason: undefined, error_code: code,
      error_class: 'ChatAuthorizationRefusedError', error_detail: 'chat model budget refused' }));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: failure, refusal: {
      status, error_code: code, error_class: 'ChatAuthorizationRefusedError', error_detail: 'chat model budget refused' } });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('still fails a rejected receipt from a gateway that predates error_class and error_detail', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...receipt, status: 'rejected', handoff: 'not_started', reservation_status: 'not_reserved',
      usage: null, content: undefined, stop_reason: undefined, error_code: 'model_admission_refused' }));
    const failure = client.invokeTextModel('summary-1', request);
    await expect(failure).rejects.toMatchObject({ code: 'denied', refusal: { status: 'rejected', error_code: 'model_admission_refused' } });
    await expect(failure).rejects.not.toHaveProperty('refusal.error_class');
  });

  it('propagates refusal without a direct-provider fallback', async () => {
    fetchMock.mockResolvedValueOnce(json({ detail: { error: 'chat_scope_refused' } }, 404));
    await expect(client.invokeTextModel('summary-1', request)).rejects.toMatchObject({ code: 'denied' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
