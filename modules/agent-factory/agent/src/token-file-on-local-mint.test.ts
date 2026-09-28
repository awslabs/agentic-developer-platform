/**
 * Every token refresh must rewrite the token FILE, not just the env vars
 * (issue #4369, root cause B).
 *
 * Since #1469, `git-askpass-helper` and `gh-wrapper` read `/tmp/.adp-gh-token`
 * FIRST and only fall back to `$GITHUB_TOKEN` when the file is absent. The
 * broker-mode refresh paths were updated for that (#4272); the two local-mint
 * paths were not — they set the three env vars and returned. So on the local-mint
 * path a refresh logged success while every subsequent `git push` / `gh pr create`
 * inside the SDK subprocess kept authenticating with the stale file contents. That
 * is why "Token refreshed proactively" appeared in the logs of runs that were
 * simultaneously 401ing.
 *
 * These tests assert against a REAL file on disk rather than a `writeFileSync`
 * spy: the contract the subprocess depends on is "the bytes at that path are the
 * current token", and only reading the file back actually proves it.
 */

/* eslint-disable @typescript-eslint/no-explicit-any */

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const mockSign = jest.fn(() => 'fake.jwt.token');
jest.mock('jsonwebtoken', () => ({ __esModule: true, default: { sign: mockSign } }));

const mockFetch = jest.fn();
global.fetch = mockFetch as any;

const APP_ID = '12345';
const INSTALLATION_ID = 987;
const OWNER = 'test-org';
const STALE_TOKEN = 'ghs_STALE_TOKEN_FROM_AN_HOUR_AGO';
const FRESH_TOKEN = 'ghs_FRESHLY_MINTED_TOKEN';

function jsonResponse(body: unknown, ok = true, url = "") {
  return { ok, status: ok ? 200 : 404, url, redirected: false, json: async () => body };
}

describe('local-mint refresh keeps the token file in step', () => {
  let tokenFilePath: string;
  let tmpDir: string;
  let refreshGitHubToken: () => Promise<void>;

  beforeEach(() => {
    jest.resetModules();
    jest.clearAllMocks();

    // Point the token file at a real temp path, then load the modules so they
    // capture it (TOKEN_FILE_PATH is resolved at import time).
    tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'adp-token-test-'));
    tokenFilePath = path.join(tmpDir, '.adp-gh-token');
    process.env.ADP_TOKEN_FILE = tokenFilePath;

    // Local-mint mode: a private key present, broker explicitly off.
    process.env.GH_APP_ID = APP_ID;
    process.env.GH_APP_PRIVATE_KEY = 'fake-private-key';
    process.env.REPO_OWNER = OWNER;
    process.env.REPO_NAME = 'test-repo';
    delete process.env.ADP_GH_TOKEN_BROKER_ENABLED;
    delete process.env.ADP_TOKEN_MODE;

    // The state at the moment of the bug: a stale token on disk and in the env.
    fs.writeFileSync(tokenFilePath, STALE_TOKEN, { mode: 0o600 });
    process.env.GH_TOKEN = STALE_TOKEN;
    process.env.GITHUB_TOKEN = STALE_TOKEN;
    process.env.GH_APP_TOKEN = STALE_TOKEN;

    mockFetch.mockImplementation(async (url: string, init?: any) => {
      const u = String(url);
      if (init?.method === 'POST' && u.includes('/access_tokens')) {
        return jsonResponse({ token: FRESH_TOKEN });
      }
      // Installation resolution ladder (#4071) — the run's own org resolves.
      if (u === `https://api.github.com/orgs/${OWNER}/installation`) {
        return jsonResponse({ id: INSTALLATION_ID, account: { login: OWNER } }, true, u);
      }
      if (u.includes('/app/installations')) {
        return jsonResponse([{ id: INSTALLATION_ID, account: { login: OWNER } }]);
      }
      return jsonResponse({}, false);
    });

    // eslint-disable-next-line @typescript-eslint/no-var-requires
    refreshGitHubToken = require('./utils/ghPost').refreshGitHubToken;
  });

  afterEach(() => {
    fs.rmSync(tmpDir, { recursive: true, force: true });
    delete process.env.ADP_TOKEN_FILE;
  });

  it('writes the freshly minted token to the file the subprocess reads', async () => {
    await refreshGitHubToken();

    // Pre-fix, this file still held STALE_TOKEN and every subprocess git/gh 401ed
    // for the rest of the run.
    expect(fs.readFileSync(tokenFilePath, 'utf-8')).toBe(FRESH_TOKEN);
  });

  it('leaves the file and the env vars agreeing', async () => {
    await refreshGitHubToken();

    // Divergence between the two is the whole bug: the worker's own calls (env)
    // succeeded while the subprocess's calls (file) failed, which is what made the
    // failure so hard to attribute.
    expect(fs.readFileSync(tokenFilePath, 'utf-8')).toBe(process.env.GITHUB_TOKEN);
    expect(process.env.GH_TOKEN).toBe(FRESH_TOKEN);
    expect(process.env.GH_APP_TOKEN).toBe(FRESH_TOKEN);
  });

  it('does not clobber the file when no token could be minted', async () => {
    // A failed mint must leave the existing token in place — overwriting it with
    // an empty string would turn a recoverable stale-token run into an
    // immediately-broken one.
    mockFetch.mockImplementation(async (url: string, init?: any) => {
      const u = String(url);
      if (init?.method === 'POST' && u.includes('/access_tokens')) {
        return jsonResponse({}, true); // no `token` field
      }
      if (u === `https://api.github.com/orgs/${OWNER}/installation`) {
        return jsonResponse({ id: INSTALLATION_ID, account: { login: OWNER } }, true, u);
      }
      return jsonResponse({}, false);
    });

    await refreshGitHubToken();

    expect(fs.readFileSync(tokenFilePath, 'utf-8')).toBe(STALE_TOKEN);
  });

  it('honours PAT mode by leaving the file untouched', async () => {
    // #3385: in PAT mode the entrypoint deliberately resolved a PAT and omits the
    // GH_APP_* vars so nothing re-mints over it.
    process.env.ADP_TOKEN_MODE = 'pat';

    await refreshGitHubToken();

    expect(fs.readFileSync(tokenFilePath, 'utf-8')).toBe(STALE_TOKEN);
    expect(mockFetch).not.toHaveBeenCalled();
  });
});
