import { parseLegacyRetry, selectLegacyRetry } from './legacy-retry';
import type { CommentEnvelope } from './comment-authority';

const now = Date.parse('2026-10-16T08:00:00Z');
const comment = (body: string, id = '51'): CommentEnvelope => ({
  id, body, author: 'alice', authorId: 42, authorType: 'User',
  createdAt: new Date(now - 1_000).toISOString(), updatedAt: new Date(now - 1_000).toISOString(),
});
const context = { owner: 'source', repo: 'control', runId: 'retry-run',
  effectRepo: { owner: 'target', repo: 'effect' }, tokenProvider: async () => 'fixture-token', now: () => now };

describe('SCN-07 legacy retry decision', () => {
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });
  const writer = () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write', user: { id: 42 } }) });
  };

  it('permits exact retry and multiline guidance only for the matching event author in both repositories', async () => {
    writer();
    const selected = await selectLegacyRetry([comment('/retry\nFollow the new plan')], context, '51', 42, now);
    expect(selected).toEqual({ guidance: 'Follow the new plan' });
    expect(global.fetch).toHaveBeenCalledTimes(2);
    expect(await selectLegacyRetry([comment('/retry')], context, '51', 42, now)).toEqual({});
  });

  it('rejects substrings, mismatched event IDs, bots, edits and old history before lock influence', async () => {
    writer();
    for (const body of ['please /retry', '/retrying', '/retryPlan']) {
      expect(parseLegacyRetry(body)).toBeNull();
    }
    expect(await selectLegacyRetry([comment('/retry')], context, '99', 42, now)).toBeNull();
    expect(await selectLegacyRetry([comment('/retry')], context, '51', 99, now)).toBeNull();
    expect(await selectLegacyRetry([{ ...comment('/retry'), authorType: 'Bot' }], context, '51', 42, now)).toBeNull();
    expect(await selectLegacyRetry([{ ...comment('/retry'), updatedAt: new Date(now).toISOString() }], context, '51', 42, now)).toBeNull();
    expect(await selectLegacyRetry([{ ...comment('/retry'), createdAt: new Date(now - 301_000).toISOString() }], context, '51', 42, now)).toBeNull();
  });

  it('denies read-only and provider-unavailable actors, including effect-only writers', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'read' }) });
    expect(await selectLegacyRetry([comment('/retry bypass lock')], context, '51', 42, now)).toBeNull();
    global.fetch = jest.fn(async (url: string) => ({ ok: true, json: async () =>
      ({ permission: url.includes('/source/control/') ? 'read' : 'write' }) })) as unknown as typeof fetch;
    expect(await selectLegacyRetry([comment('/retry bypass lock')], context, '51', 42, now)).toBeNull();
    global.fetch = jest.fn().mockRejectedValue(new Error('provider unavailable'));
    expect(await selectLegacyRetry([comment('/retry bypass lock')], context, '51', 42, now)).toBeNull();
  });
});
