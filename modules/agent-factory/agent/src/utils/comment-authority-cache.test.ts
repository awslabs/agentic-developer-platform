import { authorizeComment, CommentAuthorityCache, type CommentEnvelope } from './comment-authority';

describe('SCN-02 bounded comment authority', () => {
  const originalFetch = global.fetch;
  let now: number;
  let token: string;
  let comment: CommentEnvelope;
  let context: Parameters<typeof authorizeComment>[1];

  beforeEach(() => {
    now = Date.parse('2026-10-16T08:00:00Z');
    token = 'fixture-one';
    comment = { id: 'node-id', author: 'alice', authorId: 42, authorType: 'User', body: '/stop',
      createdAt: new Date(now).toISOString(), updatedAt: new Date(now).toISOString() };
    context = { owner: 'owner', repo: 'repo', runId: 'run-1', cache: new CommentAuthorityCache(),
      tokenProvider: async () => token, now: () => now };
  });
  afterEach(() => { global.fetch = originalFetch; });

  it('expires positive permission after 60 seconds and forces fresh approval verification', async () => {
    let permission = 'write';
    global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ permission, user: { id: 42 } }) })) as unknown as typeof fetch;
    expect((await authorizeComment(comment, context)).allowed).toBe(true);
    permission = 'read';
    now += 59_999;
    expect((await authorizeComment(comment, context)).allowed).toBe(true);
    expect((await authorizeComment(comment, context, true)).allowed).toBe(false);
    permission = 'write';
    now += 15_001;
    expect((await authorizeComment(comment, context)).allowed).toBe(true);
    expect(global.fetch).toHaveBeenCalledTimes(3);
  });

  it('expires negative permission after 15 seconds and invalidates on token rotation', async () => {
    let permission = 'read';
    global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ permission }) })) as unknown as typeof fetch;
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    permission = 'write';
    now += 14_999;
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    now += 1;
    expect((await authorizeComment(comment, context)).allowed).toBe(true);
    token = 'fixture-two';
    permission = 'triage';
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    expect(global.fetch).toHaveBeenCalledTimes(3);
  });

  it.each([401, 403, 429, 500, undefined])('denies HTTP %s and retries after five seconds', async status => {
    let broken = true;
    global.fetch = jest.fn(async () => broken
      ? status === undefined ? Promise.reject(new Error('private transport details')) : { ok: false, status }
      : { ok: true, json: async () => ({ permission: 'admin' }) }) as unknown as typeof fetch;
    expect(await authorizeComment(comment, context)).toEqual(expect.objectContaining({ allowed: false, reason: 'permission_unavailable' }));
    broken = false;
    now += 4_999;
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    now += 1;
    expect((await authorizeComment(comment, context)).allowed).toBe(true);
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('rejects identity mismatches, edits, future dates and malformed grants', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write', user: { id: 88 } }) });
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    comment.updatedAt = new Date(now + 1_000).toISOString();
    expect(await authorizeComment(comment, context)).toEqual({ allowed: false, reason: 'stale_comment' });
    comment.updatedAt = comment.createdAt;
    comment.createdAt = new Date(now + 120_000).toISOString();
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    comment.createdAt = comment.updatedAt;
    context.cache = new CommentAuthorityCache();
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: { unexpected: true } }) });
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
  });

  it('requires writer permission in both control and effect repositories', async () => {
    context.effectRepo = { owner: 'other', repo: 'effect' };
    global.fetch = jest.fn(async (url: string) => ({ ok: true,
      json: async () => ({ permission: url.includes('/other/effect/') ? 'read' : 'write' }) })) as unknown as typeof fetch;
    expect((await authorizeComment(comment, context)).allowed).toBe(false);
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('evicts the oldest entry after 256 actors without retaining a grant forever', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    for (let authorId = 1; authorId <= 257; authorId++) {
      comment.authorId = authorId;
      expect((await authorizeComment(comment, context)).allowed).toBe(true);
    }
    comment.authorId = 1;
    await authorizeComment(comment, context);
    expect(global.fetch).toHaveBeenCalledTimes(258);
  });

  it('coalesces simultaneous checks', async () => {
    let release!: (value: { ok: boolean; json: () => Promise<{ permission: string }> }) => void;
    global.fetch = jest.fn(() => new Promise<{ ok: boolean; json: () => Promise<{ permission: string }> }>(resolve => { release = resolve; })) as unknown as typeof fetch;
    const first = authorizeComment(comment, context);
    const second = authorizeComment(comment, context);
    await Promise.resolve();
    release({ ok: true, json: async () => ({ permission: 'write' }) });
    expect((await Promise.all([first, second])).every(decision => decision.allowed)).toBe(true);
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
