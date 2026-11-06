import { bindPostedApproval, checkPostedApproval, waitForPostedApproval,
  fetchApprovalComments, fetchApprovalPlan, postApprovalPlan } from './posted-approval';
import { authorizeComment, CommentAuthorityCache, type CommentEnvelope } from '../utils/comment-authority';

describe('SCN-04/05 posted plan approval', () => {
  const originalFetch = global.fetch;
  const time = new Date().toISOString();
  const receipt = { id: '10', body: 'Plan for run one', createdAt: time, updatedAt: time };
  const reply = (body: string, author = 'alice', type = 'User'): CommentEnvelope => ({
    id: '11', body, author, authorId: 42, authorType: type,
    createdAt: time, updatedAt: time,
  });
  const context = { owner: 'owner', repo: 'repo', runId: 'run-one', tokenProvider: async () => 'fixture-token' };
  const pending = () => bindPostedApproval('request-one', 'run-one', 'owner', 'repo', 42,
    receipt.body, receipt, 15 * 60_000)!;
  afterEach(() => { global.fetch = originalFetch; });

  it.each(['admin', 'maintain', 'write'])('accepts exact current approval from a %s once', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission, user: { id: 42 } }) });
    const request = pending();
    const response = await checkPostedApproval(request, [reply('/approve request-one')], async () => receipt, context);
    expect(response).toMatchObject({ outcome: 'allowed-once', approver: 'alice' });
    expect((await checkPostedApproval(request, [reply('/approve request-one')], async () => receipt, context)).outcome).toBe('rejected');
  });

  it('scans immediately and accepts a reply posted before polling began', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    expect((await waitForPostedApproval(pending(), async () => [reply('/approve request-one')],
      async () => receipt, context, 100, 1, 1)).outcome).toBe('allowed-once');
  });

  it.each(['/approve', '/go', '/lgtm', '> /approve request-one', '/approve request-one extra',
    '/approve previous', 'before /approve request-one', '/approve request-one\n/stop'])
  ('ignores invalid approval text %s', async text => {
    global.fetch = jest.fn();
    expect((await checkPostedApproval(pending(), [reply(text)], async () => receipt, context)).outcome).toBe('pending');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('rejects feedback without interpreting it as approval', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission: 'write' }) });
    expect(await checkPostedApproval(pending(), [reply('/reject request-one revise scope')], async () => receipt, context))
      .toMatchObject({ outcome: 'rejected', feedback: 'revise scope' });
  });

  it.each(['read', 'triage', 'none'])('ignores a %s reply even if the grammar matches', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => receipt, context)).outcome).toBe('pending');
  });

  it('denies bot, edited reply, wrong run and old timestamps', async () => {
    global.fetch = jest.fn();
    expect((await checkPostedApproval(pending(), [reply('/approve request-one', 'agent[bot]', 'Bot')], async () => receipt, context)).outcome).toBe('pending');
    const edited = reply('/approve request-one');
    edited.updatedAt = new Date(Date.parse(time) + 1_000).toISOString();
    expect((await checkPostedApproval(pending(), [edited], async () => receipt, context)).outcome).toBe('pending');
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => receipt,
      { ...context, runId: 'other' })).outcome).toBe('rejected');
    const earlier = reply('/approve request-one');
    earlier.createdAt = new Date(Date.parse(time) - 1_000).toISOString();
    earlier.updatedAt = earlier.createdAt;
    expect((await checkPostedApproval(pending(), [earlier], async () => receipt, context)).outcome).toBe('pending');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('invalidates changed plan, fails closed on plan lookup and unavailable permission', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 429 });
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => receipt, context)).outcome).toBe('unavailable');
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => ({ ...receipt, body: 'edited' }), context)).outcome).toBe('rejected');
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => { throw Error('private'); }, context)).outcome).toBe('unavailable');
    expect(bindPostedApproval('request-one', 'run-one', 'owner', 'repo', 42, 'other', receipt, 100)).toBeNull();
  });

  it('posts a plan, reads the bound comment and paginates all reply pages using the fixed origin', async () => {
    const paths: string[] = [];
    const pageOne = Array.from({ length: 100 }, (_, index) => ({
      id: index + 20, body: 'unrelated', user: { id: 42, login: 'alice', type: 'User' },
      created_at: time, updated_at: time,
    }));
    global.fetch = jest.fn(async (url: string, options: RequestInit) => {
      paths.push(url);
      expect(options.redirect).toBe('error');
      if (options.method === 'POST') return { ok: true, json: async () => ({
        id: 10, body: receipt.body, created_at: time, updated_at: time,
      }) };
      if (url.endsWith('/issues/comments/10')) return { ok: true, json: async () => ({
        id: 10, body: receipt.body, created_at: time, updated_at: time,
      }) };
      return { ok: true, json: async () => url.endsWith('page=1') ? pageOne : [
        { id: 120, body: '/approve request-one', user: { id: 42, login: 'alice', type: 'User' },
          created_at: time, updated_at: time },
      ] };
    }) as unknown as typeof fetch;
    expect(await postApprovalPlan('owner', 'repo', 42, receipt.body, 'fixture-token')).toEqual(receipt);
    expect(await fetchApprovalPlan('owner', 'repo', 42, '10', 'fixture-token')).toEqual(receipt);
    expect((await fetchApprovalComments('owner', 'repo', 42, 'fixture-token')).map(item => item.id)).toHaveLength(101);
    expect(paths).toEqual([
      'https://api.github.com/repos/owner/repo/issues/42/comments',
      'https://api.github.com/repos/owner/repo/issues/comments/10',
      'https://api.github.com/repos/owner/repo/issues/42/comments?per_page=100&page=1',
      'https://api.github.com/repos/owner/repo/issues/42/comments?per_page=100&page=2',
    ]);
  });

  it('keeps hostile repository input on the fixed origin and refuses redirects for every approval request', async () => {
    const owner = 'https://attacker.invalid/path?next=//elsewhere.invalid#fragment';
    const repo = '//elsewhere.invalid\\@api.github.com';
    const paths: string[] = [];
    global.fetch = jest.fn(async (url: string, options: RequestInit) => {
      const parsed = new URL(url);
      expect(parsed.origin).toBe('https://api.github.com');
      expect(parsed.username).toBe('');
      expect(parsed.password).toBe('');
      expect(parsed.pathname.startsWith(`/repos/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}/issues/`)).toBe(true);
      expect(options.redirect).toBe('error');
      paths.push(url);
      return { ok: true, json: async () => url.includes('per_page=') ? [] : {
        id: 10, body: receipt.body, created_at: time, updated_at: time,
      } };
    }) as unknown as typeof fetch;
    await postApprovalPlan(owner, repo, 42, receipt.body, 'fixture-token');
    await fetchApprovalPlan(owner, repo, 42, '10', 'fixture-token');
    await fetchApprovalComments(owner, repo, 42, 'fixture-token');
    expect(paths).toHaveLength(3);
  });

  it('refuses a failed post or incomplete page without allowing a partial reply scan', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 503 });
    await expect(postApprovalPlan('owner', 'repo', 42, receipt.body, 'fixture-token')).rejects.toThrow();
    await expect(fetchApprovalComments('owner', 'repo', 42, 'fixture-token')).rejects.toThrow();
    expect(bindPostedApproval('request-one', 'run-one', 'owner', 'repo', 42,
      receipt.body, { ...receipt, id: 'not-rest-id' }, 100)).toBeNull();
  });

  it('does not release a plan when the writer grant was revoked after a cached control check', async () => {
    let permission = 'write';
    global.fetch = jest.fn(async () => ({ ok: true, json: async () => ({ permission }) })) as unknown as typeof fetch;
    const cache = new CommentAuthorityCache();
    const approval = reply('/approve request-one');
    expect((await authorizeComment(approval, { ...context, cache })).allowed).toBe(true);
    permission = 'read';
    expect((await checkPostedApproval(pending(), [approval], async () => receipt,
      { ...context, cache })).outcome).toBe('pending');
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  it('ignores invalid/future reply timestamps and refuses an expired or edited request', async () => {
    global.fetch = jest.fn();
    const malformed = reply('/approve request-one');
    malformed.createdAt = 'invalid';
    expect((await checkPostedApproval(pending(), [malformed], async () => receipt, context)).outcome).toBe('pending');
    const future = reply('/approve request-one');
    future.createdAt = new Date(Date.now() + 120_000).toISOString();
    future.updatedAt = future.createdAt;
    expect((await checkPostedApproval(pending(), [future], async () => receipt, context)).outcome).toBe('pending');
    const expired = pending();
    expired.expiresAt = Date.now() - 1;
    expect((await checkPostedApproval(expired, [reply('/approve request-one')], async () => receipt, context)).outcome).toBe('rejected');
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it('requires both repository scopes and a fresh lookup despite cached grants', async () => {
    let permission = 'write';
    global.fetch = jest.fn(async (url: string) => ({ ok: true, json: async () => ({
      permission: url.includes('/effect/') ? 'read' : permission,
    }) })) as unknown as typeof fetch;
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => receipt,
      { ...context, effectRepo: { owner: 'owner', repo: 'effect' } })).outcome).toBe('pending');
    permission = 'read';
    expect((await checkPostedApproval(pending(), [reply('/approve request-one')], async () => receipt, context)).outcome).toBe('pending');
  });
});
