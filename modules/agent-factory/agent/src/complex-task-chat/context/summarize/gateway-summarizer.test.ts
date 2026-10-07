import { ChatDataClient, ChatDataError } from '../../gateway/chat-data-client';
import { GatewaySummarizer } from './gateway-summarizer';

describe('gateway-only summarization', () => {
  let summarizer: GatewaySummarizer;
  let invoke: jest.SpiedFunction<ChatDataClient['invokeTextModel']>;

  beforeEach(() => {
    const client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'sandbox.token' });
    invoke = jest.spyOn(client, 'invokeTextModel').mockResolvedValue({
      text: 'Preserved art_0123456789ab', stopReason: 'end_turn', modelId: 'approved-model',
      usage: { input_tokens: 4, output_tokens: 2, estimated_usd: '0.01' },
    });
    summarizer = new GatewaySummarizer(client);
  });
  afterEach(() => jest.restoreAllMocks());

  it('uses a stable operation key and no caller-selected model for each request', async () => {
    const input = { text: 'Created art_0123456789ab', mode: 'normal' as const, targetTokens: 100 };
    expect(await summarizer.summarize(input)).toBe('Preserved art_0123456789ab');
    await summarizer.summarize(input);
    expect(invoke.mock.calls[0]).toEqual(invoke.mock.calls[1]);
    expect(invoke.mock.calls[0][0]).toMatch(/^summary_[a-f0-9]{64}$/);
    expect(invoke.mock.calls[0][1]).toMatchObject({ messages: [{ role: 'user', content: input.text }], max_tokens: 500 });
    expect(invoke.mock.calls[0][1]).not.toHaveProperty('model');
  });

  it('includes every Unicode chunk and carries prior summary forward within bounded requests', async () => {
    const text = '😀'.repeat(6001);
    await summarizer.summarize({ text, mode: 'normal', targetTokens: 1200 });
    expect(invoke).toHaveBeenCalledTimes(3);
    const messages = invoke.mock.calls.map(([, request]) => request.messages[0].content);
    expect(messages[0]).toBe('😀'.repeat(3000));
    expect(messages[1]).toBe(`Previous summary:\nPreserved art_0123456789ab\n\nNew content:\n${'😀'.repeat(3000)}`);
    expect(messages[2]).toBe('Previous summary:\nPreserved art_0123456789ab\n\nNew content:\n😀');
    for (const [, request] of invoke.mock.calls) expect(Buffer.byteLength(JSON.stringify(request))).toBeLessThan(65_536);
  });

  it.each(['denied', 'incomplete', 'unavailable'] as const)('does not turn %s into an unmetered local fallback', async code => {
    invoke.mockRejectedValueOnce(new ChatDataError(code));
    await expect(summarizer.summarize({ text: 'Original history', mode: 'normal', targetTokens: 100 })).rejects.toMatchObject({ code });
    expect(invoke).toHaveBeenCalledTimes(1);
  });

  it.each([{ text: '' }, { text: 'a'.repeat(12_001) }, { stopReason: 'max_tokens' as const }])(
    'does not store an empty, oversized or provider-truncated summary', async changed => {
      invoke.mockResolvedValueOnce({ text: 'Summary', stopReason: 'end_turn', modelId: 'approved-model',
        usage: { input_tokens: 4, output_tokens: 2, estimated_usd: '0.01' }, ...changed });
      await expect(summarizer.summarize({ text: 'Original history', mode: 'normal', targetTokens: 100 }))
        .rejects.toMatchObject({ code: 'incomplete' });
    },
  );

  it('performs only explicitly requested deterministic truncation without a model call', async () => {
    const result = await summarizer.summarize({ text: 'abcdefghij', mode: 'truncate', targetTokens: 1 });
    expect(result).toBe('ab\n\n[... truncated ...]\n\nij');
    expect(invoke).not.toHaveBeenCalled();
  });

  it('refuses an oversized chunk count before spending', async () => {
    await expect(summarizer.summarize({ text: 'a'.repeat(12_000 * 64 + 1), mode: 'normal', targetTokens: 100 }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(invoke).not.toHaveBeenCalled();
  });
});
