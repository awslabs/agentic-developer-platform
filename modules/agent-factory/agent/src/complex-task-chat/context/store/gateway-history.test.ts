import { ChatDataClient } from '../../gateway/chat-data-client';
import { GatewayHistoryStore } from './gateway-history';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const stamp = '2026-10-04T12:00:00.123456Z';
const binding = { capability: 'scoped.capability', run_id: 'run-a', session_id: 'session-a', expires_at: NOW / 1000 + 300 };
const message = { role: 'user', content: 'User input', ts: stamp, tokens: 3, parts: [{ type: 'file', artifactId: 'art_attachment' }] };
const summary = { kind: 'leaf', depth: 0, content: 'Earlier context', sourceIds: ['message-a'], earliestAt: stamp, latestAt: stamp, tokens: 2 };
const write = { idempotency_key: 'write-a', expected_version: 4, content: 'Assistant reply', tokens: 4 };
const append = { ...write, user_turn_id: 'user-accepted' };
const compact = { ...write, source_ids: ['message-a'], parent_ids: [], from_ordinal: 1, to_ordinal: 1 };
const firstItem = { ordinal: 1, type: 'msg', ref: 'message-a', tokens: 3 };

function page(entries: unknown[], cursor: string | null = null, missing: string[] = []) {
  return {
    status: cursor || missing.length ? 'partial' : entries.length ? 'ok' : 'empty', entries,
    next_cursor: cursor, observed_at: stamp,
    coverage: { source: 'session_context', complete: !cursor && !missing.length, missing_source_ids: missing },
  };
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

describe('gateway history storage port', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let store: GatewayHistoryStore;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json(binding));
    store = new GatewayHistoryStore(new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'workload.token' }));
  });

  afterEach(() => jest.restoreAllMocks());

  it('drains ordered pages without losing summaries, sparse ordinals or token counts', async () => {
    const second = { ordinal: 7, type: 'sum', ref: 'sum_abc', tokens: 2 };
    fetchMock.mockResolvedValueOnce(json({ ...page([firstItem], 'next'), version: 4 }))
      .mockResolvedValueOnce(json({ ...page([second]), version: 4 }));
    await expect(store.readSnapshot('session-a')).resolves.toEqual({ version: 4, items: [firstItem, second] });
    expect(JSON.parse(fetchMock.mock.calls[2][1]?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', limit: 100, cursor: 'next',
    });
  });

  it('distinguishes an authorized empty session', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...page([]), version: 0 }));
    await expect(store.readAllContextItems('session-a')).resolves.toEqual([]);
  });

  it.each([
    ['version', { ...page([{ ...firstItem, ordinal: 2, ref: 'message-b' }]), version: 5 }, 'conflict'],
    ['ordering', { ...page([{ ...firstItem, ordinal: 0, ref: 'message-b' }]), version: 4 }, 'invalid_response'],
    ['duplicate reference', { ...page([{ ...firstItem, ordinal: 2 }]), version: 4 }, 'invalid_response'],
    ['cursor cycle', { ...page([], 'next'), version: 4 }, 'invalid_response'],
  ])('rejects inconsistent pagination: %s', async (_name, next, code) => {
    fetchMock.mockResolvedValueOnce(json({ ...page([firstItem], 'next'), version: 4 })).mockResolvedValueOnce(json(next));
    await expect(store.readSnapshot('session-a')).rejects.toMatchObject({ code });
  });

  it('preserves message order and attachment metadata across bounded batches', async () => {
    const ids = ['one', 'two', 'three', 'four', 'five'];
    fetchMock.mockResolvedValueOnce(json(page(ids.slice(0, 4).map(ref => ({ ref, message: { ...message, content: ref } })))))
      .mockResolvedValueOnce(json(page([{ ref: 'five', message: { ...message, content: 'five' } }])));
    const messages = await store.getMessagesByIds('session-a', ids);
    expect(messages.map(entry => entry.content)).toEqual(ids);
    expect(messages.every(entry => JSON.stringify(entry.parts) === JSON.stringify(message.parts))).toBe(true);
    expect(JSON.parse(fetchMock.mock.calls[2][1]?.body as string).ids).toEqual(['five']);
  });

  it('rejects mismatched by-ID responses rather than assigning them to a different message', async () => {
    fetchMock.mockResolvedValueOnce(json(page([{ ref: 'other-message', message }])));
    await expect(store.getMessagesByIds('session-a', ['message-a'])).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('reports missing source data instead of silently returning a shortened batch', async () => {
    fetchMock.mockResolvedValueOnce(json(page([], null, ['message-a'])));
    await expect(store.getMessagesByIds('session-a', ['message-a'])).rejects.toMatchObject({ code: 'incomplete' });
  });

  it('hydrates an ordered transcript and rechecks the version after hydration', async () => {
    const items = [firstItem, { ordinal: 2, type: 'sum', ref: 'sum_abc' }];
    fetchMock.mockResolvedValueOnce(json({ ...page(items), version: 4 }))
      .mockResolvedValueOnce(json(page([{ ref: firstItem.ref, message }])))
      .mockResolvedValueOnce(json(page([{ ref: 'sum_abc', summary }])))
      .mockResolvedValueOnce(json({ ...page([firstItem], 'next'), version: 4 }));
    await expect(store.getFullTranscript('session-a')).resolves.toEqual([
      { ...firstItem, message }, { ...items[1], summary },
    ]);
  });

  it('does not return a transcript changed while its sources were hydrated', async () => {
    fetchMock.mockResolvedValueOnce(json({ ...page([firstItem]), version: 4 }))
      .mockResolvedValueOnce(json(page([{ ref: firstItem.ref, message }])))
      .mockResolvedValueOnce(json({ ...page([firstItem]), version: 5 }));
    await expect(store.getFullTranscript('session-a')).rejects.toMatchObject({ code: 'conflict' });
  });

  it('expands an opaque summary ID without inferring its session', async () => {
    fetchMock.mockResolvedValueOnce(json(page([{ ref: 'sum_abc', summary }])));
    await expect(store.getSummaryById('session-a', 'sum_abc')).resolves.toEqual(summary);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      run_id: 'run-a', session_id: 'session-a', summary_id: 'sum_abc',
    });
  });

  it('reports missing summaries separately from empty history', async () => {
    fetchMock.mockResolvedValueOnce(json(page([], null, ['sum_abc'])));
    await expect(store.getSummaryById('session-a', 'sum_abc')).rejects.toMatchObject({ code: 'incomplete' });
  });

  it.each([['denied', 404], ['expired', 410], ['conflict', 409]])('propagates %s without storage fallback', async (code, status) => {
    fetchMock.mockResolvedValueOnce(json({}, status as number));
    await expect(store.readContextItems('session-a')).rejects.toMatchObject({ code });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('rejects another session before issuing a history request', async () => {
    await expect(store.readContextItems('other-session')).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each(['run_id', 'session_id', 'user_id', 'tenant_id', 'role', 'ttl', 'parts'])('cannot supply %s in an assistant append', async field => {
    await expect(store.appendAssistant('session-a', { ...append, [field]: 'forged' })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('retries the exact assistant write after a lost response', async () => {
    const result = { message_id: 'assistant-a', ordinal: 2, version: 5 };
    fetchMock.mockRejectedValueOnce(new Error('lost response')).mockResolvedValueOnce(json(result));
    await expect(store.appendAssistant('session-a', append)).resolves.toEqual(result);
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({ ...append, run_id: 'run-a', session_id: 'session-a' });
  });

  it('does not rewrite a conflicting append with a newer version', async () => {
    fetchMock.mockResolvedValueOnce(json({}, 409));
    await expect(store.appendAssistant('session-a', append)).rejects.toMatchObject({ code: 'conflict' });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('rejects an invalid receipt version', async () => {
    fetchMock.mockResolvedValueOnce(json({ message_id: 'assistant-a', ordinal: 2, version: 7 }));
    await expect(store.appendAssistant('session-a', append)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('sends summary sources and atomic compaction bounds, not caller-owned metadata', async () => {
    fetchMock.mockResolvedValueOnce(json({ summary_id: 'sum_abc', version: 5 }));
    await expect(store.compact('session-a', compact)).resolves.toEqual({ summary_id: 'sum_abc', version: 5 });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({ ...compact, run_id: 'run-a', session_id: 'session-a' });
  });

  it('appends a standalone summary with explicit provenance', async () => {
    fetchMock.mockResolvedValueOnce(json({ summary_id: 'sum_abc', version: 5 }));
    await expect(store.appendSummary('session-a', { ...write, source_ids: ['message-a'] }))
      .resolves.toEqual({ summary_id: 'sum_abc', version: 5 });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string).parent_ids).toEqual([]);
  });

  it.each([
    { source_ids: [] }, { source_ids: ['message-a', 'message-a'] }, { from_ordinal: 2, to_ordinal: 1 }, { depth: 999 },
  ])('rejects invalid compaction requests', async changes => {
    await expect(store.compact('session-a', { ...compact, ...changes })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
