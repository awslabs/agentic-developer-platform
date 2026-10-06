import { ChatDataClient, ChatDataError } from '../gateway/chat-data-client';
import { GatewayDraftStore } from './gateway-draft-store';
import { IntentDraft } from './port';

const timestamp = '2026-10-04T12:00:00+00:00';
const draft = { intent: 'Ship a feature', updatedAt: timestamp };

function readResult(stored: IntentDraft | null = null, version = 0) {
  return {
    status: stored ? 'ok' : 'empty', entries: stored ? [{ draft: stored }] : [], version,
    next_cursor: null, observed_at: timestamp,
    coverage: { source: 'session_draft', complete: true, missing_source_ids: [] },
  };
}

describe('gateway draft port', () => {
  let client: ChatDataClient;
  let request: jest.SpiedFunction<ChatDataClient['sessionRequest']>;
  let store: GatewayDraftStore;

  beforeEach(() => {
    client = new ChatDataClient({ baseUrl: 'https://gateway.example.test', workloadToken: async () => 'synthetic.workload.token' });
    request = jest.spyOn(client, 'sessionRequest');
    store = new GatewayDraftStore(client, 'session-a');
  });

  afterEach(() => jest.restoreAllMocks());

  it('maps only a verified empty response to null and accepts owned legacy version zero', async () => {
    request.mockResolvedValueOnce(readResult()).mockResolvedValueOnce(readResult(draft));
    await expect(store.get('session-a')).resolves.toBeNull();
    await expect(store.get('session-a')).resolves.toEqual(draft);
  });

  it('replaces the complete draft using the read version and the server timestamp', async () => {
    request.mockResolvedValueOnce(readResult(draft, 2)).mockResolvedValueOnce({ draft: { updatedAt: timestamp }, version: 3 });
    await store.get('session-a');
    await expect(store.put('session-a', { updatedAt: 'model timestamp' })).resolves.toEqual({ updatedAt: timestamp });
    expect(request.mock.calls[1]).toEqual(['draft/write', 'session-a', {
      draft: {}, expected_version: 2, idempotency_key: expect.any(String),
    }]);
  });

  it('reads before a first write, and uses the successful version for subsequent different writes', async () => {
    request.mockResolvedValueOnce(readResult()).mockResolvedValueOnce({ draft, version: 1 })
      .mockResolvedValueOnce({ draft: { ...draft, intent: 'A second feature' }, version: 2 });
    await store.put('session-a', { intent: draft.intent });
    await store.put('session-a', { intent: 'A second feature' });
    expect(request.mock.calls[0][0]).toBe('draft/read');
    expect(request.mock.calls[1][2]?.expected_version).toBe(0);
    expect(request.mock.calls[2][2]?.expected_version).toBe(1);
    expect(request.mock.calls[1][2]?.idempotency_key).not.toBe(request.mock.calls[2][2]?.idempotency_key);
  });

  it('keeps the same write identity after an inconclusive response and across an identical retry', async () => {
    request.mockResolvedValueOnce(readResult()).mockRejectedValueOnce(new ChatDataError('unavailable'))
      .mockResolvedValue({ draft, version: 1 });
    await expect(store.put('session-a', { intent: draft.intent })).rejects.toMatchObject({ code: 'unavailable' });
    await expect(store.put('session-a', { intent: draft.intent })).resolves.toEqual(draft);
    await expect(store.put('session-a', { intent: draft.intent })).resolves.toEqual(draft);
    expect(request.mock.calls[1][2]).toEqual(request.mock.calls[2][2]);
    expect(request.mock.calls[2][2]).toEqual(request.mock.calls[3][2]);
  });

  it('preserves conflicts until the caller explicitly rereads the draft', async () => {
    request.mockResolvedValueOnce(readResult(draft, 1)).mockRejectedValueOnce(new ChatDataError('conflict', 409))
      .mockRejectedValueOnce(new ChatDataError('conflict', 409)).mockResolvedValueOnce(readResult(draft, 2))
      .mockResolvedValueOnce({ draft, version: 3 });
    await store.get('session-a');
    await expect(store.put('session-a', { intent: draft.intent })).rejects.toMatchObject({ code: 'conflict' });
    await expect(store.put('session-a', { intent: 'different' })).rejects.toMatchObject({ code: 'conflict' });
    expect(request.mock.calls[2][2]?.expected_version).toBe(1);
    await store.get('session-a');
    await expect(store.put('session-a', { intent: draft.intent })).resolves.toEqual(draft);
    expect(request.mock.calls[4][2]?.expected_version).toBe(2);
  });

  it('serializes local writes without letting caller mutation alter a queued request', async () => {
    request.mockResolvedValueOnce(readResult()).mockResolvedValueOnce({ draft, version: 1 }).mockResolvedValueOnce({ draft, version: 2 });
    const mutable = { outcomes: ['Original outcome'] };
    const first = store.put('session-a', { intent: draft.intent });
    const second = store.put('session-a', mutable);
    mutable.outcomes[0] = 'Mutated outcome';
    await Promise.all([first, second]);
    expect(request.mock.calls[2][2]).toMatchObject({ expected_version: 1, draft: { outcomes: ['Original outcome'] } });
  });

  it('rejects cross-session access without any transport request', async () => {
    await expect(store.get('session-b')).rejects.toMatchObject({ code: 'scope_mismatch' });
    await expect(store.put('session-b', {})).rejects.toMatchObject({ code: 'scope_mismatch' });
    expect(request).not.toHaveBeenCalled();
  });

  it.each([
    { ...readResult(), status: 'partial' },
    { ...readResult(), next_cursor: 'more' },
    { ...readResult(), coverage: { source: 'session_draft', complete: false, missing_source_ids: [] } },
    { ...readResult(), entries: [{ draft }] },
    { ...readResult(), version: 1 },
    { ...readResult(), version: -1 },
    { ...readResult(), status: 'denied' },
  ])('refuses incomplete or inconsistent reads rather than reporting an empty draft', async response => {
    request.mockResolvedValue(response);
    await expect(store.get('session-a')).rejects.toMatchObject({ code: 'invalid_response' });
    await expect(store.put('session-a', {})).rejects.toMatchObject({ code: 'invalid_response' });
    expect(request.mock.calls.every(([operation]) => operation === 'draft/read')).toBe(true);
  });

  it.each([null, {}, { draft, version: 4 }, { draft: { intent: 'Missing timestamp' }, version: 1 }])('rejects malformed write receipts', async receipt => {
    request.mockResolvedValueOnce(readResult()).mockResolvedValueOnce(receipt);
    await expect(store.put('session-a', {})).rejects.toMatchObject({ code: 'invalid_response' });
  });

  it.each([{ ownerUserId: 'victim' }, { outcomes: Array(21).fill('outcome') }, { intent: 'large'.repeat(500) }])('rejects ownership fields and oversized input', async input => {
    await expect(store.put('session-a', input as IntentDraft)).rejects.toMatchObject({ code: 'invalid_request' });
    expect(request).not.toHaveBeenCalled();
  });
});
