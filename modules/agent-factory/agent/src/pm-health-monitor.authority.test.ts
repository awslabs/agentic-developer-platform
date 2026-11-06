jest.mock('./utils/ghPost', () => ({ refreshGitHubToken: jest.fn(async () => {}), saveToS3Fallback: jest.fn() }));
jest.mock('./utils/resilientQuery', () => ({ resilientQuery: jest.fn() }));

import { configureHealthMonitor, waitForUserResponse } from './pm-health-monitor';

const originalFetch = global.fetch;
const originalToken = process.env.GITHUB_TOKEN;
const issued = Date.now() - 2_000;
const createdAt = new Date(issued).toISOString();
const repliedAt = new Date(issued + 1_000).toISOString();
const proposal = { id: '51', body: 'Health proposal for #24', createdAt, updatedAt: createdAt, runId: process.env.ADP_MESSAGE_ID || 'run-24' };
const response = (body: string, authorType = 'User', updatedAt = repliedAt) => ({
  id: 52, body, created_at: repliedAt, updated_at: updatedAt,
  user: { id: 42, login: 'alice', type: authorType },
});

function setup(body: string, permission = 'write', planBody = proposal.body, authorType = 'User') {
  global.fetch = jest.fn(async (url: string) => {
    if (url.includes('/collaborators/')) return { ok: true, json: async () => ({ permission, user: { id: 42 } }) };
    if (url.endsWith('/comments/51')) return { ok: true, json: async () =>
      ({ id: 51, body: planBody, created_at: createdAt, updated_at: createdAt }) };
    return { ok: true, json: async () => [response(body, authorType)] };
  }) as unknown as typeof fetch;
}

beforeEach(() => {
  process.env.GITHUB_TOKEN = 'fixture-token';
  configureHealthMonitor({ repoOwner: 'source', repoName: 'control', userResponseTimeout: 20, pollInterval: 1 });
});
afterEach(() => {
  global.fetch = originalFetch;
  if (originalToken === undefined) delete process.env.GITHUB_TOKEN;
  else process.env.GITHUB_TOKEN = originalToken;
});

describe('SCN-07 PM health proposal entrypoint', () => {
  it.each(['/approve', '/skip', '/retry', '/cleanup', '/hold', '/custom review first'])(
    'returns verified writer response %s only after refetching the posted proposal', async body => {
      setup(body);
      const result = await waitForUserResponse(24, new Date(createdAt), proposal);
      expect(result).toMatchObject({ type: body.split(' ')[0].slice(1) });
      expect(global.fetch).toHaveBeenCalledWith(expect.stringContaining('/comments/51'), expect.anything());
    },
  );

  it.each(['/approve', '/skip', '/retry', '/cleanup', '/hold', '/custom review first'])(
    'ignores read-only response %s', async body => {
      setup(body, 'read');
      expect(await waitForUserResponse(24, new Date(createdAt), proposal)).toBeNull();
      expect((global.fetch as jest.Mock).mock.calls.some(([url]) => url.endsWith('/comments/51'))).toBe(false);
    },
  );

  it('rejects a receipt from a different run without consulting comments', async () => {
    setup('/approve');
    const original = process.env.ADP_MESSAGE_ID;
    process.env.ADP_MESSAGE_ID = 'different-run';
    try {
      expect(await waitForUserResponse(24, new Date(createdAt), proposal)).toBeNull();
      expect(global.fetch).not.toHaveBeenCalled();
    } finally {
      if (original === undefined) delete process.env.ADP_MESSAGE_ID;
      else process.env.ADP_MESSAGE_ID = original;
    }
  });

  it('refuses an altered proposal, bot, missing receipt and provider failure without returning a command', async () => {
    setup('/cleanup', 'write', 'altered proposal');
    expect(await waitForUserResponse(24, new Date(createdAt), proposal)).toBeNull();
    setup('/cleanup', 'write', proposal.body, 'Bot');
    expect(await waitForUserResponse(24, new Date(createdAt), proposal)).toBeNull();
    expect(await waitForUserResponse(24, new Date(createdAt))).toBeNull();
    global.fetch = jest.fn().mockRejectedValue(new Error('provider error'));
    expect(await waitForUserResponse(24, new Date(createdAt), proposal)).toBeNull();
  });
});
