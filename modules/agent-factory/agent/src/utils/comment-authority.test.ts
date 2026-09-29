import { parseReassessmentResponse } from '../reassessment';
import { hasRepositoryWritePermission, parsePlanApproval } from './comment-authority';

describe('comment authority', () => {
  const originalFetch = global.fetch;
  afterEach(() => { global.fetch = originalFetch; });

  it.each(['admin', 'maintain', 'write'])('accepts current %s permission', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', 'fixture-token')).toBe(true);
    expect(global.fetch).toHaveBeenCalledWith('https://api.github.com/repos/owner/repo/collaborators/alice/permission', expect.any(Object));
  });
  it.each(['read', 'triage', 'none', '', 'unknown'])('rejects %s permission', async permission => {
    global.fetch = jest.fn().mockResolvedValue({ ok: true, json: async () => ({ permission }) });
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', 'fixture-token')).toBe(false);
  });
  it('fails closed on lookup failure, missing token and bot identity', async () => {
    global.fetch = jest.fn().mockRejectedValue(new Error('network failed'));
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', 'token')).toBe(false);
    global.fetch = jest.fn().mockResolvedValue({ ok: false });
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', 'token')).toBe(false);
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', '')).toBe(false);
    expect(await hasRepositoryWritePermission('owner', 'repo', 'app[bot]', 'token')).toBe(false);
  });
  it('rejects redirected permission responses instead of granting write access', async () => {
    // Model native fetch's redirect policy: following would return a forged grant.
    global.fetch = jest.fn().mockImplementation(async (_url, init) => {
      if (init.redirect === 'error') throw new TypeError('redirect refused');
      return { ok: true, json: async () => ({ permission: 'admin' }) };
    });
    expect(await hasRepositoryWritePermission('owner', 'repo', 'alice', 'fixture')).toBe(false);
    expect(global.fetch).toHaveBeenCalledTimes(1);
    expect(global.fetch).toHaveBeenCalledWith(
      'https://api.github.com/repos/owner/repo/collaborators/alice/permission',
      expect.objectContaining({ redirect: 'error' }),
    );
  });

  it('keeps URL-like repository input inside the fixed GitHub origin', async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false });
    await hasRepositoryWritePermission('//attacker.example', 'repo?x=#y', 'alice', 'fixture');
    const requested = new URL((global.fetch as jest.Mock).mock.calls[0][0]);
    expect(requested.origin).toBe('https://api.github.com');
    expect(requested.search).toBe('');
    expect(requested.hash).toBe('');
  });

  it('binds whole commands to the current plan', () => {
    expect(parsePlanApproval('/approve plan-123', 'plan-123')).toEqual({ approved: true, feedback: '' });
    expect(parsePlanApproval('/reject plan-123 change this', 'plan-123')).toEqual({ approved: false, feedback: 'change this' });
    for (const body of ['/approve', 'approved', 'quoted /approve plan-123', '/approve old-plan', '/approve plan-123 extra', '```\n/approve plan-123\n```']) {
      expect(parsePlanApproval(body, 'plan-123')).toBeNull();
    }
  });
});


describe('reassessment command grammar', () => {
  it('does not treat quoted or prefixed command text as an approval', () => {
    for (const body of ['/approved', '/skip-anything', 'please /approve', '> /approve', '/action 1 trailing text', '/retry #2 trailing text']) {
      expect(parseReassessmentResponse(body).action).toBe('unknown');
    }
    expect(parseReassessmentResponse('/approve').action).toBe('approve_all');
    expect(parseReassessmentResponse('/action 1,2')).toEqual({ action: 'specific_actions', actionNumbers: [1, 2] });
  });
});
