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
import { isBrokerEnabled } from './lib/githubTokenBroker';
import { isMediatedRun } from './mediated-github-config';

/** The agent env entrypoint.py produces in BROKER mode: flag set, no key. */
const BROKER_ENV: NodeJS.ProcessEnv = {
  GH_APP_ID: '99001',
  GH_APP_INSTALLATION_ID: '555001',
  REPO_OWNER: 'acme-corp',
  REPO_NAME: 'flagship-app',
  ADP_GH_TOKEN_BROKER_ENABLED: '1',
};

/**
 * The agent env `entrypoint._withhold_write_token` produces for a mediated run in
 * the authority cohort (#5223).
 *
 * Every field here is what withholding actually leaves behind, and that is the
 * point: no token variables, but `GH_APP_ID`, `GH_APP_INSTALLATION_ID` and
 * `REPO_OWNER` deliberately survive (public identifiers, needed for the bot commit
 * identity), and `ADP_AGENT_AUTHORITY_ENABLED` survives because the whole authority
 * transport keys off it. `ADP_GH_TOKEN_BROKER_ENABLED` is absent — popped by
 * withholding — which is exactly why the broker predicate alone cannot be trusted
 * to stop a re-mint: `isBrokerEnabled` returns true for the authority flag on its
 * own.
 */
const MEDIATED_ENV: NodeJS.ProcessEnv = {
  GH_APP_ID: '99001',
  GH_APP_INSTALLATION_ID: '555001',
  REPO_OWNER: 'acme-corp',
  REPO_NAME: 'flagship-app',
  ADP_AGENT_AUTHORITY_ENABLED: 'true',
  ADP_TOKEN_MODE: 'mediated',
  ADP_MEDIATED_GITHUB_ENABLED: 'true',
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


  describe('mediated runs — the withheld token must never come back (#5223)', () => {
    it('refuses to initialise even though broker mode and every identifier survive', () => {
      // The precise gap this closes. `isBrokerEnabled` is TRUE here (the authority
      // flag alone sets it), the app id, installation and owner all survive
      // withholding, and ADP_TOKEN_MODE is "mediated" — not "pat" — so the old
      // 'pat'-only gate fell straight through and the manager initialised. It then
      // minted through the broker within seconds of startup.
      expect(isBrokerEnabled(MEDIATED_ENV)).toBe(true);
      expect(MEDIATED_ENV.GH_APP_ID).toBeTruthy();
      expect(MEDIATED_ENV.GH_APP_INSTALLATION_ID).toBeTruthy();
      expect(MEDIATED_ENV.REPO_OWNER).toBeTruthy();

      expect(canInitTokenManager(MEDIATED_ENV)).toBe(false);
    });

    it('refuses on the feature flag alone, without ADP_TOKEN_MODE', () => {
      // Two independent signals, so changing one does not silently reopen the path.
      const { ADP_TOKEN_MODE: _mode, ...withoutMode } = MEDIATED_ENV;
      expect(canInitTokenManager(withoutMode)).toBe(false);
    });

    it.each(['1', 'true', 'yes'])(
      'refuses for the spelling %s that entrypoint.py also accepts',
      value => {
        const { ADP_TOKEN_MODE: _mode, ...env } = MEDIATED_ENV;
        expect(canInitTokenManager({ ...env, ADP_MEDIATED_GITHUB_ENABLED: value })).toBe(false);
      },
    );

    it('still initialises for a non-mediated brokered run', () => {
      // The guard must not disable refresh for the ordinary hosted run, which is
      // the failure this file was originally written to catch.
      expect(canInitTokenManager(BROKER_ENV)).toBe(true);
    });

    it('adopts the PAT when the deployment flag is on but the run is not mediated', () => {
      // The cross-boundary half of the single per-run decision (#5223).
      //
      // ADP_MEDIATED_GITHUB_ENABLED is a POD-level variable, so a PAT run shares a
      // deployment with mediated runs and used to inherit it. This side reads that
      // raw variable, so the PAT run's agent took the mediated branch: no token
      // adopted, every mint refused, and `getRuntimeGitHubToken` throwing — while
      // the run was in fact holding the user's own valid credential.
      //
      // entrypoint now normalises the variable away for any run it did not decide to
      // mediate, so what arrives here is an ordinary PAT env. This asserts on the
      // env as produced: absent flag, ADP_TOKEN_MODE 'pat'.
      const PAT_ENV: NodeJS.ProcessEnv = {
        GH_APP_ID: '99001',
        GH_APP_INSTALLATION_ID: '555001',
        REPO_OWNER: 'acme-corp',
        REPO_NAME: 'flagship-app',
        ADP_TOKEN_MODE: 'pat',
        GITHUB_TOKEN: 'ghp_the_users_own_credential',
      };

      expect(isMediatedRun(PAT_ENV)).toBe(false);

      // And the inverse: had the flag leaked through, this run would have been
      // treated as mediated — which is the bug, stated as a test so the
      // normalisation cannot be dropped without a failure here.
      expect(isMediatedRun({ ...PAT_ENV, ADP_MEDIATED_GITHUB_ENABLED: 'true' })).toBe(true);
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

describe('mediated runs: no path restores a token to the env or the disk (#5223)', () => {
  const savedEnv = { ...process.env };
  const TOKEN_FILE = '/tmp/.adp-gh-token-mediated-guard-test';

  beforeEach(() => {
    jest.resetModules();
    mockFetchBrokeredToken.mockReset();
    mockCreateAppAuth.mockReset();
    try {
      require('fs').unlinkSync(TOKEN_FILE);
    } catch {
      /* absent is the expected case */
    }
  });

  afterEach(() => {
    for (const k of Object.keys(process.env)) if (!(k in savedEnv)) delete process.env[k];
    Object.assign(process.env, savedEnv);
    try {
      require('fs').unlinkSync(TOKEN_FILE);
    } catch {
      /* nothing to clean up */
    }
  });

  it('a forced refresh cannot write a token file or re-export the env vars', async () => {
    // The predicate is bypassed deliberately: initTokenManager is called with
    // explicit options, as agent-pm and getRuntimeGitHubToken do. This proves the
    // guard is at the WRITE, not only at the door — the broker is even primed to
    // return a usable token, so anything less than a refusal lands it on disk.
    // Cleared first: the assertions below must observe what publishToken did or
    // did not write, not whatever the developer's shell happened to export.
    delete process.env.GH_TOKEN;
    delete process.env.GITHUB_TOKEN;
    delete process.env.GH_APP_TOKEN;
    Object.assign(process.env, MEDIATED_ENV);
    process.env.ADP_TOKEN_FILE = TOKEN_FILE;
    mockFetchBrokeredToken.mockResolvedValue({
      token: 'ghs_should_never_be_published',
      expiresAt: new Date(Date.now() + 55 * 60 * 1000),
    });

    let tr: typeof import('./token-refresh');
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      tr = require('./token-refresh');
    });

    tr!.initTokenManager({
      appId: process.env.GH_APP_ID!,
      owner: process.env.REPO_OWNER!,
      repo: process.env.REPO_NAME,
      installationId: process.env.GH_APP_INSTALLATION_ID,
      brokerMode: true,
    });

    await expect(tr!.getToken()).rejects.toThrow(/[Mm]ediated/);

    // Not even fetched: no token material enters this process at all, so none can
    // reach a log line or an error string on the way to being refused.
    expect(mockFetchBrokeredToken).not.toHaveBeenCalled();

    // The three ways the agent would actually reach GitHub, all still empty.
    expect(require('fs').existsSync(TOKEN_FILE)).toBe(false);
    expect(process.env.GH_TOKEN).toBeUndefined();
    expect(process.env.GITHUB_TOKEN).toBeUndefined();
    expect(process.env.GH_APP_TOKEN).toBeUndefined();
  });

  it('getRuntimeGitHubToken refuses instead of minting', async () => {
    Object.assign(process.env, MEDIATED_ENV);
    process.env.ADP_TOKEN_FILE = TOKEN_FILE;
    mockFetchBrokeredToken.mockResolvedValue({
      token: 'ghs_should_never_be_published',
      expiresAt: new Date(Date.now() + 55 * 60 * 1000),
    });

    let tr: typeof import('./token-refresh');
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      tr = require('./token-refresh');
    });

    await expect(tr!.getRuntimeGitHubToken()).rejects.toThrow(/[Mm]ediated/);
    expect(mockFetchBrokeredToken).not.toHaveBeenCalled();
    expect(require('fs').existsSync(TOKEN_FILE)).toBe(false);
  });

  it('adoptBootstrapToken cannot republish a surviving bootstrap token', async () => {
    // Belt and braces: withholding pops GH_APP_TOKEN, but if it ever survived,
    // adoption must not be the path that writes it back to disk.
    Object.assign(process.env, MEDIATED_ENV);
    process.env.ADP_TOKEN_FILE = TOKEN_FILE;
    process.env.GH_APP_TOKEN = 'ghs_stale_bootstrap';
    process.env.GH_APP_TOKEN_EXPIRES_AT = new Date(Date.now() + 30 * 60 * 1000).toISOString();

    let tr: typeof import('./token-refresh');
    jest.isolateModules(() => {
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      tr = require('./token-refresh');
    });

    expect(() => tr!.adoptBootstrapToken()).toThrow(/[Mm]ediated/);
    expect(require('fs').existsSync(TOKEN_FILE)).toBe(false);
  });
});
