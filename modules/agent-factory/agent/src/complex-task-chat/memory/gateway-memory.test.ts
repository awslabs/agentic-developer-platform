import { ChatDataClient } from '../gateway/chat-data-client';
import { GatewayMemoryProvider } from './gateway-memory';

const NOW = Date.parse('2026-10-04T12:00:00Z');
const ID = `mem_${'a'.repeat(32)}`;
const OTHER_ID = `mem_${'b'.repeat(32)}`;
const record = {
  id: ID, version: 1, content: 'A remembered fact', kind: 'fact', tags: [],
  scope: { user: 'owner', tenant: 'tenant' }, labels: { persona: 'reviewer' },
  source: { sessionId: 'session-a', runId: 'run-a' }, createdAt: new Date(NOW).toISOString(), updatedAt: new Date(NOW).toISOString(),
};

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function page(entries: unknown[] = [], cursor: string | null = null, missing: string[] = []) {
  return {
    status: cursor || missing.length ? 'partial' : entries.length ? 'ok' : 'empty', entries,
    next_cursor: cursor, observed_at: new Date(NOW).toISOString(),
    coverage: { source: 'owned_memory', complete: !cursor && !missing.length, missing_source_ids: missing },
  };
}

describe('gateway memory provider through the scoped transport', () => {
  let fetchMock: jest.SpiedFunction<typeof fetch>;
  let client: ChatDataClient;
  let provider: GatewayMemoryProvider;

  beforeEach(() => {
    jest.spyOn(Date, 'now').mockReturnValue(NOW);
    fetchMock = jest.spyOn(globalThis, 'fetch').mockResolvedValueOnce(json({
      capability: 'synthetic.capability', run_id: 'run-a', session_id: 'session-a', expires_at: NOW / 1000 + 300,
    }));
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token' });
    provider = new GatewayMemoryProvider(client);
  });

  afterEach(() => jest.restoreAllMocks());

  it('drains empty filtered pages without sending ownership hints as authority', async () => {
    fetchMock.mockResolvedValueOnce(json(page([], 'next'))).mockResolvedValueOnce(json(page([record])));
    const results = await provider.retrieve({ query: 'fact', scope: { user: 'victim', tenant: 'other', persona: 'reviewer' }, kinds: ['fact'] });
    expect(results[0].scope).toEqual({ user: 'owner', tenant: 'tenant', persona: 'reviewer' });
    expect(results[0].version).toBe(1);
    const requests = fetchMock.mock.calls.slice(1).map(([, options]) => JSON.parse(options?.body as string));
    expect(requests).toEqual([undefined, 'next'].map(cursor => ({
      query: 'fact', kinds: ['fact'], labels: { persona: 'reviewer' }, limit: 4, run_id: 'run-a', ...(cursor ? { cursor } : {}),
    })));
  });

  it('honours both record and token budgets without changing page filters', async () => {
    fetchMock.mockResolvedValueOnce(json(page([record, { ...record, id: OTHER_ID }], 'more')));
    expect(await provider.retrieve({ query: '', tokenBudget: Math.ceil(record.content.length / 4) })).toHaveLength(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('returns empty only for complete empty searches', async () => {
    fetchMock.mockResolvedValueOnce(json(page()));
    await expect(provider.retrieve({ query: '' })).resolves.toEqual([]);
  });

  it.each([page([], null, [ID]), page([record], 'more', [OTHER_ID])])('reports missing source coverage instead of silently returning available rows', async response => {
    fetchMock.mockResolvedValueOnce(json(response));
    await expect(provider.retrieve({ query: '' })).rejects.toMatchObject({ code: 'incomplete' });
  });

  it('rejects repeated cursors', async () => {
    fetchMock.mockImplementation(async () => json(page([], 'same')));
    await expect(provider.retrieve({ query: '' })).rejects.toMatchObject({ code: 'invalid_response' });
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('uses the gateway receipt and rereads scrubbed, authoritative content after a save', async () => {
    fetchMock.mockResolvedValueOnce(json({ memory_id: ID, version: 1 })).mockResolvedValueOnce(json(page([{ ...record, content: '[REDACTED]' }])));
    const saved = await provider.save({ content: 'private input', kind: 'fact', scope: { user: 'victim', tenant: 'other', persona: 'reviewer' }, source: { sessionId: 'forged' }, updatedAt: 'forged' });
    expect(saved.content).toBe('[REDACTED]');
    expect(saved.scope.user).toBe('owner');
    expect(saved.source).toEqual({ sessionId: 'session-a', runId: 'run-a' });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toEqual({
      content: 'private input', kind: 'fact', tags: [], labels: { persona: 'reviewer' }, expected_version: 0,
      idempotency_key: expect.stringMatching(/^[a-f0-9]{64}$/), run_id: 'run-a',
    });
  });

  it('keeps a stable save key across lost responses and caller retries', async () => {
    fetchMock.mockRejectedValueOnce(new Error('lost')).mockResolvedValueOnce(json({ memory_id: ID, version: 1 }))
      .mockResolvedValueOnce(json(page([record]))).mockResolvedValueOnce(json({ memory_id: ID, version: 1 })).mockResolvedValueOnce(json(page([record])));
    await provider.save({ content: record.content, scope: {} });
    await provider.save({ content: record.content, scope: {} });
    expect(fetchMock.mock.calls[1][1]?.body).toBe(fetchMock.mock.calls[2][1]?.body);
    expect(fetchMock.mock.calls[2][1]?.body).toBe(fetchMock.mock.calls[4][1]?.body);
  });

  it('updates at the caller-observed version and never retries a version conflict', async () => {
    fetchMock.mockResolvedValueOnce(json({ memory_id: ID, version: 2 })).mockResolvedValueOnce(json(page([{ ...record, version: 2 }])))
      .mockResolvedValueOnce(json({ detail: 'conflict' }, 409));
    await expect(provider.update(ID, 1, { content: record.content, scope: {} })).resolves.toMatchObject({ version: 2 });
    await expect(provider.update(ID, 1, { content: 'stale', scope: {} })).rejects.toMatchObject({ code: 'conflict', status: 409 });
    expect(fetchMock).toHaveBeenCalledTimes(4);
    expect(JSON.parse(fetchMock.mock.calls[3][1]?.body as string)).toMatchObject({ memory_id: ID, expected_version: 1 });
  });

  it('reports a concurrent change between the write receipt and readback', async () => {
    fetchMock.mockResolvedValueOnce(json({ memory_id: ID, version: 2 })).mockResolvedValueOnce(json(page([{ ...record, version: 3 }])));
    await expect(provider.update(ID, 1, { content: record.content, scope: {} })).rejects.toMatchObject({ code: 'conflict' });
  });

  it.each([401, 403, 404, 410])('preserves HTTP %s instead of reporting empty memory', async status => {
    fetchMock.mockResolvedValueOnce(json({ detail: 'refused' }, status));
    await expect(provider.retrieve({ query: '' })).rejects.toMatchObject({ status });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it.each(['run_id', 'user_id', 'tenant_id', 'session_id', 'scope', 'headers'])('rejects supplied %s at the transport boundary', async field => {
    await expect(client.runRequest('memory/search', { [field]: 'forged' })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it.each([page(), page([{ ...record, id: OTHER_ID }]), page([record], 'more')])('rejects an inconsistent by-id response', async response => {
    fetchMock.mockResolvedValueOnce(json(response));
    await expect(provider.read(ID)).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it('distinguishes a missing by-id source from an empty search', async () => {
    fetchMock.mockResolvedValueOnce(json(page([], null, [ID])));
    await expect(provider.read(ID)).rejects.toMatchObject({ code: 'incomplete' });
  });

  it('does not expose delete and rejects invalid versions and unpersisted metadata', async () => {
    expect(provider.capabilities()).toMatchObject({ delete: false, ttl: true });
    await expect(provider.update(ID, 0, { content: 'invalid', scope: {} })).rejects.toMatchObject({ code: 'invalid_request' });
    await expect(provider.save({ content: 'invalid', scope: {}, metadata: { hidden: true } })).rejects.toMatchObject({ code: 'invalid_request' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('binds tool labels to the trusted persona and ignores model-supplied scope', async () => {
    fetchMock.mockResolvedValueOnce(json({ memory_id: ID, version: 1 })).mockResolvedValueOnce(json(page([record])));
    await provider.tools({ user: 'owner', tenant: 'tenant', persona: 'reviewer' }).find(tool => tool.name === 'save_learning')!.handler({
      content: record.content, scope: { user: 'victim', persona: 'admin' }, labels: { persona: 'admin' },
    });
    expect(JSON.parse(fetchMock.mock.calls[1][1]?.body as string)).toMatchObject({ labels: { persona: 'reviewer' }, kind: 'learning' });
  });
});
