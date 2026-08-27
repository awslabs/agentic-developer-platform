/**
 * The 🔴-5 guard: the token manager must still initialise with no private key.
 *
 * Issue #4272. Both call sites (agent-worker.ts, agent-pm.ts) used to gate
 * initTokenManager on `GH_APP_PRIVATE_KEY` being present. Removing the key from
 * the agent environment therefore had a silent, delayed failure mode: the token
 * manager never initialises, no refresh is ever scheduled, and the run dies at
 * the 1-hour mark with `401 Bad credentials` from git/gh — which reads as a flaky
 * agent, not as a config change. Nothing at deploy time would surface it.
 *
 * Those call sites invoke `main()` at import time, so a test cannot import them.
 * That is exactly why the predicate was extracted into `canInitTokenManager()`:
 * one tested decision instead of two hand-maintained copies that can drift. These
 * tests pin it, and pin that a refresh really is scheduled once it returns true.
 */

const mockFetchBrokeredToken = jest.fn();

jest.mock('./lib/githubTokenBroker', () => {
  const actual = jest.requireActual('./lib/githubTokenBroker');
  return {
    isBrokerEnabled: actual.isBrokerEnabled,
    fetchBrokeredToken: (...args: unknown[]) => mockFetchBrokeredToken(...args),
  };
});

const mockCreateAppAuth = jest.fn();
jest.mock('@octokit/auth-app', () => ({
  createAppAuth: (...args: unknown[]) => mockCreateAppAuth(...args),
}));

import { canInitTokenManager } from './token-refresh';

/** The agent env entrypoint.py produces in BROKER mode: flag set, no key. */
const BROKER_ENV: NodeJS.ProcessEnv = {
  GH_APP_ID: '99001',
  GH_APP_INSTALLATION_ID: '555001',
  REPO_OWNER: 'acme-corp',
  REPO_NAME: 'flagship-app',
  ADP_GH_TOKEN_BROKER_ENABLED: '1',
};

/** The agent env entrypoint.py produces with the flag OFF: key, no flag. */
const LEGACY_ENV: NodeJS.ProcessEnv = {
  GH_APP_ID: '99001',
  GH_APP_INSTALLATION_ID: '555001',
  REPO_OWNER: 'acme-corp',
  REPO_NAME: 'flagship-app',
  GH_APP_PRIVATE_KEY: '-----BEGIN RSA PRIVATE KEY-----\nlocal\n-----END RSA PRIVATE KEY-----',
};

describe('canInitTokenManager', () => {
  describe('broker mode — no private key in the environment', () => {
    it('initialises with the exact env entrypoint.py exports in broker mode', () => {
      // THE regression guard. If this goes false, every hosted run silently loses
      // token refresh and dies at ~1h.
      expect(canInitTokenManager(BROKER_ENV)).toBe(true);
      expect(BROKER_ENV.GH_APP_PRIVATE_KEY).toBeUndefined();
    });

    it.each(['1', 'true', 'yes'])('accepts flag value %s', flag => {
      expect(
        canInitTokenManager({ ...BROKER_ENV, ADP_GH_TOKEN_BROKER_ENABLED: flag }),
      ).toBe(true);
    });

    it('refuses when the installation id is absent — a refresh would only throw', () => {
      // generateNewToken() will not guess the installation, so initialising here
      // would move the 1-hour death into a confusing stack trace instead of
      // preventing it. Better to say so at startup.
      const env = { ...BROKER_ENV };
      delete env.GH_APP_INSTALLATION_ID;
      expect(canInitTokenManager(env)).toBe(false);
    });

    it('refuses when the repo owner is absent', () => {
      const env = { ...BROKER_ENV };
      delete env.REPO_OWNER;
      expect(canInitTokenManager(env)).toBe(false);
    });
  });

  describe('legacy mode — unchanged behavior with the flag off', () => {
    it('initialises with a private key and no flag', () => {
      expect(canInitTokenManager(LEGACY_ENV)).toBe(true);
    });

    it('refuses with the flag off and no private key', () => {
      // The pre-#4272 behavior, preserved: nothing to mint with, so no refresh
      // path exists. This is what must NOT happen in broker mode.
      const env = { ...LEGACY_ENV };
      delete env.GH_APP_PRIVATE_KEY;
      expect(canInitTokenManager(env)).toBe(false);
    });

    it('accepts the GH_APP_KEY alias that agent-worker.ts also reads', () => {
      const env = { ...LEGACY_ENV };
      delete env.GH_APP_PRIVATE_KEY;
      env.GH_APP_KEY = '-----BEGIN RSA PRIVATE KEY-----\nalias\n-----END RSA PRIVATE KEY-----';
      expect(canInitTokenManager(env)).toBe(true);
    });

    it('does not treat an off-ish flag value as broker mode', () => {
      // A misspelled/false flag must fall back to requiring the key, not skip the
      // check on both sides and initialise something that can never refresh.
      const env = { ...BROKER_ENV, ADP_GH_TOKEN_BROKER_ENABLED: 'false' };
      expect(canInitTokenManager(env)).toBe(false);
    });
  });

  describe('always false without an app id', () => {
    it.each([BROKER_ENV, LEGACY_ENV])('refuses when GH_APP_ID is missing', base => {
      const env = { ...base };
      delete env.GH_APP_ID;
      expect(canInitTokenManager(env)).toBe(false);
    });
  });
});

describe('a refresh really is scheduled with no key in the environment', () => {
  let savedEnv: Record<string, string | undefined>;
  const ENV_KEYS = [
    'GH_APP_ID',
    'GH_APP_INSTALLATION_ID',
    'GH_APP_PRIVATE_KEY',
    'GH_APP_KEY',
    'REPO_OWNER',
    'REPO_NAME',
    'ADP_GH_TOKEN_BROKER_ENABLED',
    'ADP_TOKEN_FILE',
    'GH_TOKEN',
    'GITHUB_TOKEN',
    'GH_APP_TOKEN',
  ];

  beforeEach(() => {
    mockFetchBrokeredToken.mockReset();
    mockCreateAppAuth.mockReset();
    savedEnv = {};
    for (const k of ENV_KEYS) {
      savedEnv[k] = process.env[k];
      delete process.env[k];
    }
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

  it('predicate true -> init succeeds -> getToken() mints via the gatekeeper', async () => {
    // End-to-end on the decision: not just "the predicate says yes", but that
    // saying yes actually yields a working refresh with the key absent. Reading
    // the real process.env is deliberate — it is what the call sites do.
    Object.assign(process.env, BROKER_ENV);
    process.env.ADP_TOKEN_FILE = '/tmp/.adp-gh-token-init-guard-test';
    mockFetchBrokeredToken.mockResolvedValue({
      token: 'ghs_brokered',
      expiresAt: new Date(Date.now() + 55 * 60 * 1000),
    });

    let tr: typeof import('./token-refresh');
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      tr = require('./token-refresh');
    });

    expect(process.env.GH_APP_PRIVATE_KEY).toBeUndefined();
    expect(tr!.canInitTokenManager()).toBe(true);

    tr!.initTokenManager({
      appId: process.env.GH_APP_ID!,
      owner: process.env.REPO_OWNER!,
      repo: process.env.REPO_NAME,
      installationId: process.env.GH_APP_INSTALLATION_ID,
    });

    // A refresh is genuinely scheduled: needsRefresh() is true before the first
    // mint, and the mint goes through the gatekeeper rather than the local path.
    expect(tr!.needsRefresh()).toBe(true);
    await expect(tr!.getToken()).resolves.toBe('ghs_brokered');
    expect(mockCreateAppAuth).not.toHaveBeenCalled();
    expect(tr!.needsRefresh()).toBe(false);
  });
});
