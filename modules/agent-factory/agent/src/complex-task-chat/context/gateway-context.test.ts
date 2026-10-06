import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { GatewayContextManager } from './gateway-context';
import { DEFAULT_LCM_CONFIG } from './lcm/config';
import { GatewayHistoryStore } from './store/gateway-history';

const stamp = '2026-10-04T12:00:00Z';
const scope = { run_id: 'run-a', session_id: 'session-a' };
const turn = {
  sessionId: 'session-a', userMessage: { role: 'user' as const, content: 'Trusted ingress input' },
  assistantMessage: { role: 'assistant' as const, content: 'Assistant response' },
};
const page = {
  entries: [], version: 4, status: 'empty' as const, next_cursor: null, observed_at: stamp,
  coverage: { source: 'session_context' as const, complete: true, missing_source_ids: [] },
};

function items(count: number) {
  return Array.from({ length: count }, (_, index) => ({ ordinal: index + 1, type: 'msg' as const, ref: `message-${index}`, tokens: 10 }));
}

describe('gateway context manager', () => {
  let client: ChatDataClient;
  let manager: GatewayContextManager;
  let summarizer: { summarize: jest.Mock<Promise<string>, [unknown]> };
  let readPage: jest.SpiedFunction<GatewayHistoryStore['readPage']>;
  let readSnapshot: jest.SpiedFunction<GatewayHistoryStore['readSnapshot']>;
  let readMessages: jest.SpiedFunction<GatewayHistoryStore['getMessagesByIds']>;
  let readSummary: jest.SpiedFunction<GatewayHistoryStore['getSummaryById']>;
  let append: jest.SpiedFunction<GatewayHistoryStore['appendAssistant']>;
  let compact: jest.SpiedFunction<GatewayHistoryStore['compact']>;

  beforeEach(() => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' });
    jest.spyOn(client, 'sessionScope').mockResolvedValue(scope);
    jest.spyOn(console, 'warn').mockImplementation(() => undefined);
    readPage = jest.spyOn(GatewayHistoryStore.prototype, 'readPage').mockResolvedValue(page);
    jest.spyOn(GatewayHistoryStore.prototype, 'acceptedUserTurn').mockResolvedValue({ message_id: 'user-accepted', ordinal: 101 });
    readSnapshot = jest.spyOn(GatewayHistoryStore.prototype, 'readSnapshot').mockResolvedValue({ version: 4, items: [] });
    readMessages = jest.spyOn(GatewayHistoryStore.prototype, 'getMessagesByIds').mockImplementation(async (_session, ids) => ids.map(reference => ({
      role: 'user', content: reference, tokens: 10, ts: stamp, parts: [{ type: 'file', artifactId: 'art_test' }],
    })));
    readSummary = jest.spyOn(GatewayHistoryStore.prototype, 'getSummaryById').mockResolvedValue({
      kind: 'leaf', depth: 0, content: 'Compressed context', tokens: 3,
      sourceIds: ['message-0'], earliestAt: stamp, latestAt: stamp,
    });
    append = jest.spyOn(GatewayHistoryStore.prototype, 'appendAssistant').mockResolvedValue({ message_id: 'message-a', ordinal: 10, version: 5 });
    compact = jest.spyOn(GatewayHistoryStore.prototype, 'compact').mockResolvedValue({ summary_id: 'sum_opaque', version: 6 });
    summarizer = { summarize: jest.fn(async (_input: unknown) => 'Brief') };
    manager = new GatewayContextManager(client, summarizer, { ...DEFAULT_LCM_CONFIG, freshTailCount: 1, leafChunkTokens: 20 });
  });

  afterEach(() => jest.restoreAllMocks());

  it('asserts access through the gateway without forwarding caller identity or creating a header', async () => {
    await manager.assertOwnership('session-a', 'forged-user', 'forged-tenant', { teamId: 'forged-team' });
    expect(readPage).toHaveBeenCalledWith('session-a', { limit: 1 });
    expect(append).not.toHaveBeenCalled();
  });

  it('preserves chronological assembly, summary markers and the protected fresh tail', async () => {
    readSnapshot.mockResolvedValue({ version: 4, items: [items(1)[0], { ordinal: 2, type: 'sum', ref: 'sum_opaque', tokens: 3 }, items(3)[2]] });
    const assembled = await manager.assemble({ sessionId: 'session-a', userMessage: 'New input', tokenBudget: 100 });
    expect(assembled.messages[0]).toEqual({ role: 'user', content: 'message-0' });
    expect(assembled.messages[1].content).toContain('<summary id="sum_opaque"');
    expect(assembled.messages[2]).toEqual({ role: 'user', content: 'message-2' });
    expect(assembled.meta).toEqual({ rawMessageCount: 2, summaryCount: 1, estimatedTokens: 23, compactionTriggered: false });
    const bounded = await manager.assemble({ sessionId: 'session-a', userMessage: 'New input', tokenBudget: 10 });
    expect(bounded.messages).toEqual([{ role: 'user', content: 'message-2' }]);
  });

  it('represents authorized empty history without inventing a session', async () => {
    await expect(manager.assemble({ sessionId: 'session-a', userMessage: 'New input', tokenBudget: 100 }))
      .resolves.toMatchObject({ messages: [], meta: { rawMessageCount: 0, summaryCount: 0 } });
    expect(append).not.toHaveBeenCalled();
  });

  it('excludes the admitted current turn from context supplied alongside the current prompt', async () => {
    jest.mocked(GatewayHistoryStore.prototype.acceptedUserTurn).mockResolvedValue({ message_id: 'message-2', ordinal: 3 });
    readSnapshot.mockResolvedValue({ version: 4, items: items(4) });
    const assembled = await manager.assemble({ sessionId: 'session-a', userMessage: 'Current prompt', tokenBudget: 100 });
    expect(assembled.messages.map(message => message.content)).toEqual(['message-0', 'message-1']);
  });

  it('refuses recording when the accepted user source is missing', async () => {
    jest.mocked(GatewayHistoryStore.prototype.acceptedUserTurn).mockRejectedValue(new ChatDataError('unavailable'));
    await expect(manager.record(turn)).rejects.toMatchObject({ code: 'unavailable' });
    expect(append).not.toHaveBeenCalled();
  });

  it.each(['denied', 'incomplete', 'expired'] as const)('does not turn %s history into an empty session', async code => {
    readSnapshot.mockRejectedValue(new ChatDataError(code));
    await expect(manager.assemble({ sessionId: 'session-a', userMessage: 'Input', tokenBudget: 100 })).rejects.toMatchObject({ code });
  });

  it('refuses assembly if the timeline changes during hydration', async () => {
    readSnapshot.mockResolvedValue({ version: 4, items: items(1) });
    readPage.mockResolvedValue({ ...page, version: 5 });
    await expect(manager.assemble({ sessionId: 'session-a', userMessage: 'Input', tokenBudget: 100 }))
      .rejects.toMatchObject({ code: 'conflict' });
  });

  it('records only assistant content against the version used for assembly', async () => {
    await manager.assemble({ sessionId: 'session-a', userMessage: 'Input', tokenBudget: 100 });
    readPage.mockResolvedValue({ ...page, version: 10 });
    await manager.record(turn);
    expect(append).toHaveBeenCalledWith('session-a', {
      idempotency_key: expect.stringMatching(/^assistant_[a-f0-9]{64}$/), expected_version: 4,
      user_turn_id: 'user-accepted',
      content: 'Assistant response', tokens: 5,
    });
    expect(JSON.stringify(append.mock.calls)).not.toContain('Trusted ingress input');
  });

  it('reuses the exact pending write after unavailable responses and reauthorizes successful retries', async () => {
    append.mockRejectedValueOnce(new ChatDataError('unavailable'));
    await expect(manager.record(turn)).rejects.toMatchObject({ code: 'unavailable' });
    readPage.mockResolvedValue({ ...page, version: 5 });
    await manager.record(turn);
    await manager.record(turn);
    expect(append).toHaveBeenCalledTimes(3);
    expect(append.mock.calls[0]).toEqual(append.mock.calls[1]);
    expect(append.mock.calls[1]).toEqual(append.mock.calls[2]);
    expect(readSnapshot).toHaveBeenCalledTimes(1);
    expect(client.sessionScope).toHaveBeenCalledTimes(3);
  });

  it('serializes concurrent retries into the same idempotent assistant write', async () => {
    await Promise.all([manager.record(turn), manager.record(turn)]);
    expect(append.mock.calls[0]).toEqual(append.mock.calls[1]);
    expect(readSnapshot).toHaveBeenCalledTimes(1);
  });

  it('does not overwrite a version conflict or accept a second response for the same run', async () => {
    append.mockRejectedValueOnce(new ChatDataError('conflict'));
    await expect(manager.record(turn)).rejects.toMatchObject({ code: 'conflict' });
    await expect(manager.record({ ...turn, assistantMessage: { role: 'assistant', content: 'Changed response' } }))
      .rejects.toMatchObject({ code: 'conflict' });
    expect(append).toHaveBeenCalledTimes(1);
    expect(compact).not.toHaveBeenCalled();
  });

  it('does not bypass revocation on replay', async () => {
    await manager.record(turn);
    append.mockRejectedValueOnce(new ChatDataError('denied'));
    await expect(manager.record(turn)).rejects.toMatchObject({ code: 'denied' });
  });

  it('rejects an unbound session before any write', async () => {
    await expect(manager.record({ ...turn, sessionId: 'other-session' })).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(append).not.toHaveBeenCalled();
  });

  it('rejects model-created user-role output', async () => {
    await expect(manager.record({ ...turn, assistantMessage: { role: 'user', content: 'Impersonation' } }))
      .rejects.toMatchObject({ code: 'invalid_request' });
    expect(append).not.toHaveBeenCalled();
  });

  it('compacts at most the gateway atomic limit and retains the newest message', async () => {
    readSnapshot.mockResolvedValue({ version: 5, items: items(100) });
    await manager.record(turn);
    expect(compact).toHaveBeenCalledWith('session-a', {
      idempotency_key: expect.stringMatching(/^compact_[a-f0-9]{64}$/), expected_version: 5,
      from_ordinal: 1, to_ordinal: 94, content: 'Brief', tokens: 2,
      source_ids: items(94).map(item => item.ref), parent_ids: [],
    });
    expect(readMessages).toHaveBeenCalledWith('session-a', items(94).map(item => item.ref));
    await manager.record(turn);
    expect(compact).toHaveBeenCalledTimes(1);
  });

  it('does not roll back a committed assistant response when compaction conflicts', async () => {
    readSnapshot.mockResolvedValue({ version: 5, items: items(4) });
    compact.mockRejectedValue(new ChatDataError('conflict'));
    await expect(manager.record(turn)).resolves.toBeUndefined();
    expect(append).toHaveBeenCalledTimes(1);
    expect(compact).toHaveBeenCalledTimes(1);
    expect(console.warn).toHaveBeenCalledWith('[gateway-context] Assistant recorded; compaction unavailable');
  });

  it('does not starve compaction when the backlog exceeds the atomic chunk limit', async () => {
    manager = new GatewayContextManager(client, summarizer, { ...DEFAULT_LCM_CONFIG, freshTailCount: 1, leafChunkTokens: 990 });
    readSnapshot.mockResolvedValue({ version: 5, items: items(100) });
    await manager.record(turn);
    expect(compact).toHaveBeenCalledWith('session-a', expect.objectContaining({ from_ordinal: 1, to_ordinal: 94 }));
  });

  it('keeps original history when the injected summarizer fails', async () => {
    readSnapshot.mockResolvedValue({ version: 5, items: items(4) });
    summarizer.summarize.mockRejectedValue(new Error('Model unavailable'));
    await expect(manager.record(turn)).resolves.toBeUndefined();
    expect(compact).not.toHaveBeenCalled();
  });

  it('expands opaque summary IDs only in the server-bound session', async () => {
    const tool = manager.tools()[0];
    const expanded = await tool.handler({ summary_id: 'sum_other_session_opaque' });
    expect(readSummary).toHaveBeenCalledWith('session-a', 'sum_other_session_opaque');
    expect(readMessages).toHaveBeenCalledWith('session-a', ['message-0']);
    expect(expanded.content[0].text).toContain(`[${stamp}] user:\nmessage-0`);
    await expect(tool.handler({ summary_id: 'sum_opaque', session_id: 'other-session' }))
      .rejects.toMatchObject({ code: 'invalid_request' });
  });

  it('preserves denial and missing-source errors from summary expansion', async () => {
    readSummary.mockRejectedValueOnce(new ChatDataError('denied'));
    await expect(manager.tools()[0].handler({ summary_id: 'sum_opaque' })).rejects.toMatchObject({ code: 'denied' });
    readMessages.mockRejectedValueOnce(new ChatDataError('incomplete'));
    await expect(manager.tools()[0].handler({ summary_id: 'sum_opaque' })).rejects.toMatchObject({ code: 'incomplete' });
  });
});
