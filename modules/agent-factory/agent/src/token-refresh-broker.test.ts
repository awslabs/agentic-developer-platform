/**
 * Broker-mode tests for token-refresh.ts (issue #4272).
 *
 * The TokenManager is what keeps a long agent run alive past GitHub's 1-hour
 * installation-token expiry. Moving the mint into the gateway means this module
 * must keep every side effect it had while no longer holding a private key:
 *
 *  - all three env tokens set (GH_TOKEN / GITHUB_TOKEN / GH_APP_TOKEN) AND the
 *    GIT_ASKPASS token file rewritten. The askpass helper prefers the FILE over
 *    $GITHUB_TOKEN, so a refresh that updates only env leaves git authenticating
 *    with a dead token — a failure that looks like a git problem, not a token one.
 *  - the local mint provably unreachable: initTokenManager drops any privateKey
 *    in broker mode, so there is nothing to fall back to even if a caller passes
 *    one.
 *  - broker failure throws. Returning a stale token would let the run drift onto
 *    a dying credential and fail later, somewhere unrelated.
 *  - expiry comes from the gateway (GitHub's own value), so needsRefresh() is
 *    scheduled off reality rather than a guess.
 */

import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';

const mockFetchBrokeredToken = jest.fn();
const mockIsBrokerEnabled = jest.fn();

jest.mock('./lib/githubTokenBroker', () => ({
  fetchBrokeredToken: (...args: unknown[]) => mockFetchBrokeredToken(...args),
  isBrokerEnabled: (...args: unknown[]) => mockIsBrokerEnabled(...args),
}));

// createAppAuth is the LOCAL mint. It must never be reached in broker mode; the
// mock exists so that a regression calls a spy instead of the network.
const mockCreateAppAuth = jest.fn();
jest.mock('@octokit/auth-app', () => ({
  createAppAuth: (...args: unknown[]) => mockCreateAppAuth(...args),
}));

const BROKERED_TOKEN = 'ghs_brokered_from_gateway';
const FAR_FUTURE = new Date(Date.now() + 55 * 60 * 1000);

describe('token-refresh broker mode', () => {
  let tokenFile: string;
  let savedEnv: Record<string, string | undefined>;
  const ENV_KEYS = [
    'ADP_TOKEN_FILE',
    'GH_TOKEN',
    'GITHUB_TOKEN',
    'GH_APP_TOKEN',
    'GH_APP_PRIVATE_KEY',
    'ADP_GH_TOKEN_BROKER_ENABLED',
    'ADP_AGENT_AUTHORITY_ENABLED',
    'ADP_TOKEN_MODE',
    'GH_APP_TOKEN_EXPIRES_AT',
  ];

  /** Re-import with a fresh module registry so module-level token state resets. */
  function loadModule() {
    let mod: typeof import('./token-refresh');
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      mod = require('./token-refresh');
    });
    return mod!;
  }

  beforeEach(() => {
    mockFetchBrokeredToken.mockReset();
    mockCreateAppAuth.mockReset();
    mockIsBrokerEnabled.mockReset().mockReturnValue(false);

    savedEnv = {};
    for (const k of ENV_KEYS) {
      savedEnv[k] = process.env[k];
      delete process.env[k];
    }
    tokenFile = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'adp-tok-')), '.adp-gh-token');
    process.env.ADP_TOKEN_FILE = tokenFile;

    jest.spyOn(console, 'log').mockImplementation(() => {});
    jest.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    for (const k of ENV_KEYS) {
      if (savedEnv[k] === undefined) delete process.env[k];
      else process.env[k] = savedEnv[k];
    }
    jest.restoreAllMocks();
  });

  function brokerOk(expiresAt: Date = FAR_FUTURE) {
    mockFetchBrokeredToken.mockResolvedValue({ token: BROKERED_TOKEN, expiresAt });
  }

  describe('refresh side effects', () => {
    it('sets all three env tokens and rewrites the askpass token file', async () => {
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        repo: 'flagship-app',
        installationId: '555001',
      });

      const token = await tr.getToken();

      expect(token).toBe(BROKERED_TOKEN);
      expect(process.env.GH_TOKEN).toBe(BROKERED_TOKEN);
      expect(process.env.GITHUB_TOKEN).toBe(BROKERED_TOKEN);
      expect(process.env.GH_APP_TOKEN).toBe(BROKERED_TOKEN);
      // The askpass helper reads the FILE first. Env-only would leave git using
      // the previous, expiring token.
      expect(fs.readFileSync(tokenFile, 'utf-8')).toBe(BROKERED_TOKEN);
    });

    it('passes the pinned installation and repo to the gatekeeper', async () => {
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        repo: 'flagship-app',
        installationId: '555001',
      });

      await tr.getToken();

      expect(mockFetchBrokeredToken).toHaveBeenCalledWith({
        installationId: '555001',
        repoOwner: 'acme-corp',
        repoName: 'flagship-app',
      });
    });

    it("schedules refresh off the gateway's expiry, not a local guess", async () => {
      // 55 minutes out with a 20-minute threshold => no refresh needed yet.
      brokerOk(new Date(Date.now() + 55 * 60 * 1000));
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();
      expect(tr.needsRefresh()).toBe(false);

      const status = tr.getTokenStatus();
      expect(status?.valid).toBe(true);
      expect(status?.expiresIn).toBeGreaterThan(30 * 60 * 1000);
    });

    it('re-mints when the brokered token is inside the refresh threshold', async () => {
      // 5 minutes left, broker-mode threshold is 20 => every call refreshes.
      brokerOk(new Date(Date.now() + 5 * 60 * 1000));
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();
      expect(tr.needsRefresh()).toBe(true);
      await tr.getToken();

      expect(mockFetchBrokeredToken).toHaveBeenCalledTimes(2);
    });

    it('defaults to a wider refresh threshold in broker mode than locally', async () => {
      // A gatekeeper round-trip can fail and need retrying; 15 minutes of
      // headroom leaves too little room to notice and recover.
      brokerOk(new Date(Date.now() + 18 * 60 * 1000));
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();

      // 18 min remaining is inside the 20-min broker threshold (would be OUTSIDE
      // the 15-min local one).
      expect(tr.needsRefresh()).toBe(true);
    });

    it('forceRefresh also updates env and the token file', async () => {
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      const token = await tr.forceRefresh();

      expect(token).toBe(BROKERED_TOKEN);
      expect(fs.readFileSync(tokenFile, 'utf-8')).toBe(BROKERED_TOKEN);
    });
  });

  describe('local mint is unreachable', () => {
    it('never calls createAppAuth in broker mode', async () => {
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();

      expect(mockCreateAppAuth).not.toHaveBeenCalled();
      expect(mockFetchBrokeredToken).toHaveBeenCalledTimes(1);
    });

    it('drops a privateKey even if a caller passes one in broker mode', async () => {
      // Defense in depth: a future caller that still threads the key through must
      // not silently re-enable the local path (and with it, key-in-process).
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        privateKey: '-----BEGIN RSA PRIVATE KEY-----\nleaked\n-----END RSA PRIVATE KEY-----',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();

      expect(mockCreateAppAuth).not.toHaveBeenCalled();
      expect(mockFetchBrokeredToken).toHaveBeenCalledTimes(1);
    });

    it('infers broker mode from the environment when not passed explicitly', async () => {
      // entrypoint.py exports ADP_GH_TOKEN_BROKER_ENABLED; a call site that
      // forgets the brokerMode option must still not attempt a local mint.
      mockIsBrokerEnabled.mockReturnValue(true);
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        owner: 'acme-corp',
        installationId: '555001',
      });

      await tr.getToken();

      expect(mockFetchBrokeredToken).toHaveBeenCalledTimes(1);
      expect(mockCreateAppAuth).not.toHaveBeenCalled();
    });
  });

  describe('failure is loud', () => {
    it('throws when the gatekeeper fails — no stale-token fallback', async () => {
      mockFetchBrokeredToken.mockRejectedValue(new Error('gateway 403 installation_binding_mismatch'));
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        installationId: '555001',
      });

      await expect(tr.getToken()).rejects.toThrow(/installation_binding_mismatch/);
      expect(mockCreateAppAuth).not.toHaveBeenCalled();
    });

    it('refuses to guess when the installation id is absent', async () => {
      brokerOk();
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        brokerMode: true,
        owner: 'acme-corp',
        // no installationId — resolving it locally needed the App JWT, i.e. the key
      });

      await expect(tr.getToken()).rejects.toThrow(/GH_APP_INSTALLATION_ID/);
      expect(mockFetchBrokeredToken).not.toHaveBeenCalled();
    });
  });

  describe('renewal isolation', () => {
    function init(tr: ReturnType<typeof loadModule>) {
      tr.initTokenManager({ appId: '1', brokerMode: true, owner: 'acme', repo: 'repo', installationId: '2' });
    }

    it('coalesces the timer, posting helper and forced refresh into one mint', async () => {
      let resolve!: (value: unknown) => void;
      mockFetchBrokeredToken.mockImplementation(() => new Promise(done => { resolve = done; }));
      const tr = loadModule(); init(tr);
      const pending = [tr.getToken(), tr.getRuntimeGitHubToken(), tr.forceRefresh()];
      expect(mockFetchBrokeredToken).toHaveBeenCalledTimes(1);
      resolve({ token: BROKERED_TOKEN, expiresAt: FAR_FUTURE });
      expect(await Promise.all(pending)).toEqual([BROKERED_TOKEN, BROKERED_TOKEN, BROKERED_TOKEN]);
      expect(fs.statSync(tokenFile).mode & 0o777).toBe(0o600);
      expect(fs.readdirSync(path.dirname(tokenFile))).toEqual(['.adp-gh-token']);
    });

    it('keeps all consumers on the previous token if atomic publication fails', async () => {
      const tr = loadModule(); init(tr); brokerOk();
      await tr.getToken();
      const before = tr.getTokenStatus();
      fs.unlinkSync(tokenFile);
      fs.mkdirSync(tokenFile); // rename over a directory must fail on the real filesystem
      mockFetchBrokeredToken.mockResolvedValue({ token: 'replacement', expiresAt: FAR_FUTURE });
      await expect(tr.forceRefresh()).rejects.toThrow('Failed to publish');
      expect(process.env.GITHUB_TOKEN).toBe(BROKERED_TOKEN);
      expect(tr.getTokenStatus()?.refreshedAt).toEqual(before?.refreshedAt);
      expect(fs.readdirSync(path.dirname(tokenFile))).toEqual(['.adp-gh-token']);
      fs.rmdirSync(tokenFile);
      await expect(tr.forceRefresh()).resolves.toBe('replacement');
      expect(fs.readFileSync(tokenFile, 'utf8')).toBe('replacement');
    });

    it('adopts the actual bootstrap expiry without minting or guessing a new hour', async () => {
      const tr = loadModule(); init(tr);
      const expiry = new Date(Date.now() + 30 * 60 * 1000);
      tr.adoptBootstrapToken({ GH_APP_TOKEN: 'bootstrap', GH_APP_TOKEN_EXPIRES_AT: expiry.toISOString() });
      expect(await tr.getToken()).toBe('bootstrap');
      expect(tr.getTokenStatus()?.expiresIn).toBeLessThanOrEqual(30 * 60 * 1000);
      expect(mockFetchBrokeredToken).not.toHaveBeenCalled();
    });

    it('renews for two hours and publishes for a child with a frozen environment', async () => {
      jest.useFakeTimers();
      try {
        const tr = loadModule(); init(tr);
        let generation = 0;
        mockFetchBrokeredToken.mockImplementation(async () => ({
          token: `generation-${++generation}`, expiresAt: new Date(Date.now() + 60 * 60 * 1000),
        }));
        await tr.getToken();
        const childEnv = { ...process.env };
        const { execFileSync } = require('child_process');
        const askpass = path.resolve(__dirname, '../../agent-worker-image/git-askpass-helper');
        for (let minute = 5; minute <= 120; minute += 5) {
          jest.setSystemTime(Date.now() + 5 * 60 * 1000);
          await tr.getRuntimeGitHubToken();
          expect(tr.getTokenStatus()?.valid).toBe(true);
          expect(execFileSync('bash', [askpass, 'Password for https://github.com'], { env: childEnv, encoding: 'utf8' }).trim())
            .toBe(`generation-${generation}`);
        }
        expect(generation).toBeGreaterThanOrEqual(3);
        expect(childEnv.GITHUB_TOKEN).toBe('generation-1');
      } finally { jest.useRealTimers(); }
    });

    it('never enables App refresh for PAT execution even with inherited App settings', async () => {
      process.env.ADP_TOKEN_MODE = 'pat';
      process.env.GITHUB_TOKEN = 'personal-token';
      const tr = loadModule();
      expect(tr.canInitTokenManager({ ADP_TOKEN_MODE: 'pat', GH_APP_ID: '1', GH_APP_KEY: 'key', REPO_OWNER: 'acme' })).toBe(false);
      expect(() => init(tr)).toThrow('PAT');
      expect(await tr.getRuntimeGitHubToken()).toBe('personal-token');
      expect(mockFetchBrokeredToken).not.toHaveBeenCalled();
    });

    it('cannot disable broker mode for an authority worker via an init option', async () => {
      process.env.ADP_AGENT_AUTHORITY_ENABLED = 'true';
      const tr = loadModule(); brokerOk();
      tr.initTokenManager({ appId: '1', privateKey: 'key', brokerMode: false, owner: 'acme', installationId: '2' });
      await tr.getToken();
      expect(mockCreateAppAuth).not.toHaveBeenCalled();
    });
  });

  describe('non-broker mode is unchanged', () => {
    it('still uses the local mint when the broker is off', async () => {
      mockCreateAppAuth.mockReturnValue(
        jest.fn().mockResolvedValue({
          token: 'ghs_local',
          expiresAt: new Date(Date.now() + 60 * 60 * 1000).toISOString(),
        }),
      );
      const tr = loadModule();
      tr.initTokenManager({
        appId: '99001',
        privateKey: '-----BEGIN RSA PRIVATE KEY-----\nlocal\n-----END RSA PRIVATE KEY-----',
        owner: 'acme-corp',
        installationId: '555001',
      });

      const token = await tr.getToken();

      expect(token).toBe('ghs_local');
      expect(mockCreateAppAuth).toHaveBeenCalled();
      expect(mockFetchBrokeredToken).not.toHaveBeenCalled();
    });
  });
});
