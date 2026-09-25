/**
 * Behavioral regression tests for refreshGitHubToken() — issue #4071, finding #13.
 *
 * The bug: refreshGitHubToken() listed every installation of the GitHub App and
 * minted unconditionally against `installations[0]`. On an App serving more than
 * one tenant that is an arbitrary installation, so the minted token grants
 * read/write on a *foreign* customer's repositories — and because this function
 * overwrites GH_TOKEN/GITHUB_TOKEN/GH_APP_TOKEN process-wide, it also clobbered
 * the correct token agent-worker.ts had already resolved.
 *
 * These tests assert the OUTCOME (which installation was charged, what landed in
 * the env), not the source text. They fail on pre-fix code.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

const mockSign = jest.fn(() => 'fake.jwt.token');
jest.mock('jsonwebtoken', () => ({ __esModule: true, default: { sign: mockSign } }));

import { refreshGitHubToken } from './ghPost';

const mockFetch = jest.fn();
global.fetch = (async (...args: any[]) => {
  const response = await mockFetch(...args);
  return response.url === undefined ? { ...response, url: String(args[0]) } : response;
}) as any;

// Two installations, deliberately ordered so that installations[0] is the
// FOREIGN tenant — this is what GitHub returns (newest-first).
const FOREIGN_INSTALLATION_ID = 111; // org-b — must never be minted for org-a
const OWN_INSTALLATION_ID = 222; // org-a — the run's own installation

const INSTALLATIONS = [
  { id: FOREIGN_INSTALLATION_ID, account: { login: 'org-b' } },
  { id: OWN_INSTALLATION_ID, account: { login: 'org-a' } },
];

const TOKEN_FOR: Record<number, string> = {
  [FOREIGN_INSTALLATION_ID]: 'ghs_TOKEN_FOR_FOREIGN_ORG_B',
  [OWN_INSTALLATION_ID]: 'ghs_TOKEN_FOR_OWN_ORG_A',
};

function jsonResponse(body: unknown, ok = true) {
  return { ok, status: ok ? 200 : 404, json: async () => body };
}

/** URLs of every access_tokens POST that was issued. */
function mintedUrls(): string[] {
  return mockFetch.mock.calls
    .filter(([, init]) => (init as any)?.method === 'POST')
    .map(([url]) => String(url));
}

/**
 * Route the GitHub API endpoints refreshGitHubToken() may touch.
 * `resolvableOwners` controls which /orgs|/users/{owner}/installation lookups
 * succeed, so we can exercise each rung of the ladder.
 */
function installGitHubApiMock(opts: { resolvableOwners?: Record<string, number> } = {}) {
  const resolvable = opts.resolvableOwners ?? {};
  mockFetch.mockImplementation(async (url: string, init?: any) => {
    const target = String(url);

    if (init?.method === 'POST') {
      const m = target.match(/\/app\/installations\/(\d+)\/access_tokens$/);
      const id = m ? Number(m[1]) : -1;
      return jsonResponse({ token: TOKEN_FOR[id] ?? `ghs_UNKNOWN_INSTALLATION_${id}` });
    }
    if (/\/app\/installations$/.test(target)) {
      return jsonResponse(INSTALLATIONS);
    }
    const ownerMatch = target.match(/\/(orgs|users)\/([^/]+)\/installation$/);
    if (ownerMatch) {
      const id = resolvable[ownerMatch[2]];
      return id ? jsonResponse({ id }) : jsonResponse({ message: 'Not Found' }, false);
    }
    throw new Error(`unexpected fetch: ${target}`);
  });
}

describe('refreshGitHubToken — installation binding (issue #4071 #13)', () => {
  const originalEnv = { ...process.env };
  let warnSpy: jest.SpyInstance;

  beforeEach(() => {
    mockFetch.mockReset();
    mockSign.mockClear();
    warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});

    // Baseline: App credentials present, no PAT mode.
    delete process.env.ADP_TOKEN_MODE;
    delete process.env.GH_APP_INSTALLATION_ID;
    delete process.env.REPO_OWNER;
    delete process.env.GH_TOKEN;
    delete process.env.GITHUB_TOKEN;
    delete process.env.GH_APP_TOKEN;
    process.env.GH_APP_ID = '900001';
    process.env.GH_APP_PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----';
  });

  afterEach(() => {
    warnSpy.mockRestore();
    process.env = { ...originalEnv };
  });

  it('mints for the run\'s OWN installation, not installations[0], when resolved by REPO_OWNER', async () => {
    process.env.REPO_OWNER = 'org-a';
    installGitHubApiMock({ resolvableOwners: { 'org-a': OWN_INSTALLATION_ID } });

    await refreshGitHubToken();

    expect(process.env.GH_TOKEN).toBe(TOKEN_FOR[OWN_INSTALLATION_ID]);
    expect(process.env.GITHUB_TOKEN).toBe(TOKEN_FOR[OWN_INSTALLATION_ID]);
    expect(process.env.GH_APP_TOKEN).toBe(TOKEN_FOR[OWN_INSTALLATION_ID]);
  });

  it('never POSTs to the foreign installation\'s access_tokens endpoint', async () => {
    process.env.REPO_OWNER = 'org-a';
    installGitHubApiMock({ resolvableOwners: { 'org-a': OWN_INSTALLATION_ID } });

    await refreshGitHubToken();

    expect(mintedUrls()).not.toContain(
      `https://api.github.com/app/installations/${FOREIGN_INSTALLATION_ID}/access_tokens`
    );
    expect(mintedUrls()).toEqual([
      `https://api.github.com/app/installations/${OWN_INSTALLATION_ID}/access_tokens`,
    ]);
  });

  it('prefers GH_APP_INSTALLATION_ID outright, without listing installations', async () => {
    process.env.GH_APP_INSTALLATION_ID = String(OWN_INSTALLATION_ID);
    process.env.REPO_OWNER = 'org-a';
    installGitHubApiMock({ resolvableOwners: { 'org-a': 999 } });

    await refreshGitHubToken();

    expect(process.env.GH_TOKEN).toBe(TOKEN_FOR[OWN_INSTALLATION_ID]);
    // The authoritative id short-circuits discovery entirely.
    const listed = mockFetch.mock.calls.map(([url]) => String(url));
    expect(listed).not.toContain('https://api.github.com/app/installations');
  });

  it('falls back to the /users/{owner}/installation rung for user-owned repos', async () => {
    process.env.REPO_OWNER = 'org-a';
    // /orgs fails, /users succeeds — installGitHubApiMock resolves both kinds,
    // so restrict resolution to the user endpoint explicitly.
    mockFetch.mockImplementation(async (url: string, init?: any) => {
      const target = String(url);
      if (init?.method === 'POST') {
        const id = Number(target.match(/installations\/(\d+)\/access_tokens/)![1]);
        return jsonResponse({ token: TOKEN_FOR[id] });
      }
      if (/\/orgs\/org-a\/installation$/.test(target)) return jsonResponse({ message: 'Not Found' }, false);
      if (/\/users\/org-a\/installation$/.test(target)) return jsonResponse({ id: OWN_INSTALLATION_ID });
      if (/\/app\/installations$/.test(target)) return jsonResponse(INSTALLATIONS);
      throw new Error(`unexpected fetch: ${target}`);
    });

    await refreshGitHubToken();

    expect(process.env.GH_TOKEN).toBe(TOKEN_FOR[OWN_INSTALLATION_ID]);
    expect(mintedUrls()).not.toContain(
      `https://api.github.com/app/installations/${FOREIGN_INSTALLATION_ID}/access_tokens`
    );
  });

  it.each([undefined, '../..', 'uninstalled-owner'])('does not mint for a foreign tenant when owner %j is unresolved', async (owner) => {
    if (owner) process.env.REPO_OWNER = owner;
    process.env.GH_TOKEN = 'synthetic-existing-token';
    installGitHubApiMock(); // App-wide listing would return the foreign tenant first.
    await refreshGitHubToken();
    expect(mintedUrls()).toEqual([]);
    expect(process.env.GH_TOKEN).toBe('synthetic-existing-token');
    expect(mockFetch.mock.calls.map(([url]) => String(url))).not.toContain('https://api.github.com/app/installations');
  });

  it('does not overwrite a healthy token when the owner has no installation at all', async () => {
    // No REPO_OWNER, no GH_APP_INSTALLATION_ID and no installations => nothing
    // resolvable, so the pre-existing token must survive untouched.
    process.env.GH_TOKEN = 'ghs_ALREADY_CORRECT';
    mockFetch.mockImplementation(async (url: string) => {
      if (/\/app\/installations$/.test(String(url))) return jsonResponse([]);
      throw new Error(`unexpected fetch: ${url}`);
    });

    await refreshGitHubToken();

    expect(process.env.GH_TOKEN).toBe('ghs_ALREADY_CORRECT');
    expect(mintedUrls()).toEqual([]);
  });
});

describe('refreshGitHubToken — PAT mode guard (issue #3385 A4, carried by #4071)', () => {
  const originalEnv = { ...process.env };

  beforeEach(() => {
    mockFetch.mockReset();
    mockSign.mockClear();
    process.env.GH_APP_ID = '900001';
    process.env.GH_APP_PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----';
  });

  afterEach(() => {
    process.env = { ...originalEnv };
  });

  it('leaves an entrypoint-resolved PAT intact and mints nothing', async () => {
    process.env.ADP_TOKEN_MODE = 'pat';
    process.env.GH_TOKEN = 'ghp_USER_PAT';
    process.env.GITHUB_TOKEN = 'ghp_USER_PAT';
    process.env.REPO_OWNER = 'org-a';
    installGitHubApiMock({ resolvableOwners: { 'org-a': OWN_INSTALLATION_ID } });

    await refreshGitHubToken();

    expect(process.env.GH_TOKEN).toBe('ghp_USER_PAT');
    expect(process.env.GITHUB_TOKEN).toBe('ghp_USER_PAT');
    expect(mockFetch).not.toHaveBeenCalled();
  });
});

describe('refreshGitHubToken — failures are reported, not swallowed (issue #4071)', () => {
  const originalEnv = { ...process.env };
  let warnSpy: jest.SpyInstance;

  beforeEach(() => {
    mockFetch.mockReset();
    mockSign.mockClear();
    warnSpy = jest.spyOn(console, 'warn').mockImplementation(() => {});
    delete process.env.ADP_TOKEN_MODE;
    delete process.env.GH_APP_INSTALLATION_ID;
    process.env.GH_APP_ID = '900001';
    process.env.GH_APP_PRIVATE_KEY = '-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----';
    process.env.REPO_OWNER = 'org-a';
  });

  afterEach(() => {
    warnSpy.mockRestore();
    process.env = { ...originalEnv };
  });

  it('warns when the GitHub API call throws instead of failing silently', async () => {
    process.env.GH_APP_INSTALLATION_ID = String(OWN_INSTALLATION_ID);
    mockFetch.mockRejectedValue(new Error('getaddrinfo ENOTFOUND api.github.com'));

    await refreshGitHubToken();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('GitHub token refresh failed')
    );
  });

  it('warns when no installation can be resolved', async () => {
    mockFetch.mockImplementation(async (url: string) => {
      const target = String(url);
      if (/\/app\/installations$/.test(target)) return jsonResponse([]);
      return jsonResponse({ message: 'Not Found' }, false);
    });

    await refreshGitHubToken();

    expect(warnSpy).toHaveBeenCalledWith(
      expect.stringContaining('no installation could be resolved')
    );
  });
});
