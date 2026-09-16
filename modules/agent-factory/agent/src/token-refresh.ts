/**
 * Token Refresh Module
 *
 * Manages GitHub App installation token refresh for long-running workflows.
 * GitHub App tokens expire after 1 hour, so this module refreshes them
 * before expiration to avoid authentication failures.
 */

import { execFileSync } from 'child_process';
import { fetchBrokeredToken, isBrokerEnabled } from './lib/githubTokenBroker';
import { TOKEN_FILE_PATH, writeTokenFile } from './lib/tokenFile';

// ============================================================================
// Types
// ============================================================================

export interface TokenManagerConfig {
  appId: string;
  /**
   * The App private key, for the legacy local-mint path.
   *
   * Issue #4272: OPTIONAL, and unset in broker mode. `generateNewToken()`
   * refuses to run a local mint without it, which is what makes the local path
   * provably unreachable once the key is no longer exported into this process.
   */
  privateKey?: string;
  installationId?: string;
  owner: string;
  repo?: string;
  workDir?: string;
  refreshThresholdMs?: number; // Refresh when this much time remains (default: 15 min)
  /**
   * Issue #4272: re-mint through the gateway gatekeeper instead of locally.
   * Defaults from ADP_GH_TOKEN_BROKER_ENABLED at init.
   */
  brokerMode?: boolean;
}

export interface TokenInfo {
  token: string;
  expiresAt: Date;
  refreshedAt: Date;
}

// ============================================================================
// Token File (Option B-hybrid — issue #1469)
// ============================================================================

/**
 * Re-exported from `lib/tokenFile` so every existing importer keeps working.
 * The implementation moved to a leaf module because this one pulls in
 * `@octokit/auth-app` (ESM-only, untransformable under jest) — see that file.
 */
export { TOKEN_FILE_PATH, writeTokenFile };

// ============================================================================
// Token Manager
// ============================================================================

let currentToken: TokenInfo | null = null;
let config: TokenManagerConfig | null = null;
let refreshInFlight: Promise<string> | null = null;

/**
 * Can the token manager be initialised with the credentials in this environment?
 *
 * Issue #4272. Every call site historically wrote this predicate by hand as
 * `appId && owner && privateKey`. With the private key no longer exported in
 * broker mode, each of those copies silently goes false: the token manager never
 * initialises, no refresh is ever scheduled, and the run dies at the 1-hour mark
 * with a 401 that reads like a flaky agent rather than a config error.
 *
 * It lives here, exported and tested once, because the call sites (agent-worker,
 * agent-pm) run `main()` at import time and so cannot be imported by a test —
 * two hand-maintained copies of a security-relevant predicate that no test can
 * reach is exactly how the silent-outage path opens back up.
 *
 * @param env Environment to inspect (injectable for tests).
 * @returns true when initTokenManager() will have a working refresh path.
 */
export function canInitTokenManager(env: NodeJS.ProcessEnv = process.env): boolean {
  if (env.ADP_TOKEN_MODE === 'pat') return false;
  const appId = env.GH_APP_ID || '';
  const owner = env.REPO_OWNER || '';
  const installationId = env.GH_APP_INSTALLATION_ID || '';
  const privateKey = env.GH_APP_PRIVATE_KEY || env.GH_APP_KEY || '';

  if (!appId) return false;

  if (isBrokerEnabled(env)) {
    // The gateway holds the key, so its absence is expected rather than
    // disqualifying. But generateNewToken() refuses to guess the installation or
    // the org, so without both, every refresh would throw — initialising then
    // would only move the 1-hour death into a confusing stack trace.
    return Boolean(owner && installationId);
  }

  // Local mint: the key is mandatory. The installation is resolved from `owner`
  // via the App JWT when it was not passed explicitly, so either one suffices.
  return Boolean(privateKey && (owner || installationId));
}

/**
 * Initialize the token manager with GitHub App credentials
 */
export function initTokenManager(options: TokenManagerConfig): void {
  if (process.env.ADP_TOKEN_MODE === 'pat') {
    throw new Error('PAT execution cannot initialize GitHub App renewal');
  }
  if (refreshInFlight) throw new Error('Cannot reconfigure token manager during refresh');
  currentToken = null;
  const brokerMode = process.env.ADP_AGENT_AUTHORITY_ENABLED === 'true' || (options.brokerMode ?? isBrokerEnabled());
  config = {
    ...options,
    brokerMode,
    // Issue #4272: broker mode refreshes earlier. A gatekeeper round-trip can
    // fail and be retried; 15 minutes of headroom leaves too little room to
    // notice and recover before the token actually dies.
    refreshThresholdMs: options.refreshThresholdMs ?? (brokerMode ? 20 * 60 * 1000 : 15 * 60 * 1000),
    // Never retain a key in broker mode, even if a caller passes one. This is
    // what makes generateNewToken()'s local path unreachable.
    privateKey: brokerMode ? undefined : options.privateKey,
  };
  console.log(
    `[TokenManager] Initialized with app ID: ${options.appId}` +
      (brokerMode ? ' (broker mode — private key not held in this process)' : ''),
  );
}

/**
 * Get the installation ID for the repository
 */
async function getInstallationId(): Promise<string> {
  if (config?.installationId) {
    return config.installationId;
  }

  if (!config?.appId || !config?.privateKey || !config?.owner) {
    throw new Error('Token manager not configured');
  }

  const { createAppAuth } = await import('@octokit/auth-app');
  const auth = createAppAuth({
    appId: config.appId,
    privateKey: config.privateKey,
  });

  // Get JWT for app authentication
  const appAuth = await auth({ type: 'app' });

  // Use JWT to get installation ID
  const response = await fetch(
    `https://api.github.com/orgs/${config.owner}/installation`,
    {
      headers: {
        Authorization: `Bearer ${appAuth.token}`,
        Accept: 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
      },
    }
  );

  if (!response.ok) {
    // Try user installation if org fails
    const userResponse = await fetch(
      `https://api.github.com/users/${config.owner}/installation`,
      {
        headers: {
          Authorization: `Bearer ${appAuth.token}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
        },
      }
    );

    if (!userResponse.ok) {
      throw new Error(`Failed to get installation: ${response.status} ${await response.text()}`);
    }

    const userData = await userResponse.json() as { id: number };
    config.installationId = String(userData.id);
    return config.installationId;
  }

  const data = await response.json() as { id: number };
  config.installationId = String(data.id);
  return config.installationId;
}

/**
 * Generate a new installation access token
 */
async function generateNewToken(): Promise<TokenInfo> {
  // Issue #4272: broker mode — the gateway holds the App private key and mints
  // on our behalf, scoped to this run's org and repo. Checked FIRST so that the
  // local path below is unreachable whenever the broker is on.
  if (config?.brokerMode) {
    if (!config.installationId || !config.owner) {
      throw new Error(
        '[TokenManager] Broker mode requires GH_APP_INSTALLATION_ID and REPO_OWNER; refusing to guess.',
      );
    }

    console.log('[TokenManager] Requesting installation token from gatekeeper...');

    // Loud on failure, with no fallback: a local mint would need the key we
    // deliberately no longer have, and drifting onto a dying token is the one
    // outcome worse than a clean, visible failure.
    const brokered = await fetchBrokeredToken({
      installationId: config.installationId,
      repoOwner: config.owner,
      repoName: config.repo || '',
    });

    const brokeredInfo: TokenInfo = {
      token: brokered.token,
      expiresAt: brokered.expiresAt,
      refreshedAt: new Date(),
    };

    console.log(`[TokenManager] Brokered token received, expires at ${brokeredInfo.expiresAt.toISOString()}`);

    return brokeredInfo;
  }

  if (!config?.appId || !config?.privateKey) {
    throw new Error('Token manager not configured');
  }

  console.log('[TokenManager] Generating new installation token...');

  const installationId = await getInstallationId();

  const { createAppAuth } = await import('@octokit/auth-app');
  const auth = createAppAuth({
    appId: config.appId,
    privateKey: config.privateKey,
    installationId,
  });

  const installationAuth = await auth({ type: 'installation' });

  const tokenInfo: TokenInfo = {
    token: installationAuth.token,
    expiresAt: new Date(installationAuth.expiresAt || Date.now() + 60 * 60 * 1000),
    refreshedAt: new Date(),
  };

  console.log(`[TokenManager] New token generated, expires at ${tokenInfo.expiresAt.toISOString()}`);

  return tokenInfo;
}

/**
 * Check if the current token needs refresh
 */
export function needsRefresh(): boolean {
  if (!currentToken || !config) {
    return true;
  }

  const timeUntilExpiry = currentToken.expiresAt.getTime() - Date.now();
  const threshold = config.refreshThresholdMs ?? 15 * 60 * 1000;

  return timeUntilExpiry < threshold;
}

/**
 * Get a valid token, refreshing if necessary
 */
export async function getToken(): Promise<string> {
  if (!config) {
    throw new Error('Token manager not initialized. Call initTokenManager() first.');
  }

  if (refreshInFlight) return refreshInFlight;
  return needsRefresh() ? forceRefresh() : currentToken!.token;
}

/** Publish once for every concurrent caller, including forced refreshes. */
export async function forceRefresh(): Promise<string> {
  if (!config) throw new Error('Token manager not initialized');
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = (async () => {
    const next = await generateNewToken();
    if (!next.token || !Number.isFinite(next.expiresAt.getTime()) || next.expiresAt.getTime() <= Date.now()) {
      throw new Error('GitHub token is expired or invalid');
    }
    publishToken(next);
    return next.token;
  })();
  try {
    return await refreshInFlight;
  } finally {
    refreshInFlight = null;
  }
}

function publishToken(next: TokenInfo): void {
  writeTokenFile(next.token);
  currentToken = next;
  process.env.GH_TOKEN = next.token;
  process.env.GITHUB_TOKEN = next.token;
  process.env.GH_APP_TOKEN = next.token;
  process.env.GH_APP_TOKEN_EXPIRES_AT = next.expiresAt.toISOString();
}

/** Use the same manager from posting helpers and the proactive refresh timer. */
export async function getRuntimeGitHubToken(): Promise<string> {
  if (process.env.ADP_TOKEN_MODE === 'pat') {
    const token = process.env.GITHUB_TOKEN || process.env.GH_TOKEN;
    if (!token) throw new Error('PAT credential unavailable; reconnect the GitHub credential');
    return token;
  }
  if (!config) {
    if (!canInitTokenManager()) throw new Error('GitHub renewal configuration unavailable');
    initTokenManager({
      appId: process.env.GH_APP_ID!,
      privateKey: process.env.GH_APP_PRIVATE_KEY || process.env.GH_APP_KEY,
      installationId: process.env.GH_APP_INSTALLATION_ID,
      owner: process.env.REPO_OWNER || '',
      repo: process.env.REPO_NAME,
    });
    adoptBootstrapToken();
  }
  return getToken();
}

/** Unknown bootstrap expiry triggers a mint; it never becomes a guessed hour. */
export function adoptBootstrapToken(env: NodeJS.ProcessEnv = process.env): void {
  if (env.ADP_TOKEN_MODE === 'pat' || !env.GH_APP_TOKEN || !env.GH_APP_TOKEN_EXPIRES_AT) return;
  const expiresAt = new Date(env.GH_APP_TOKEN_EXPIRES_AT);
  if (!Number.isFinite(expiresAt.getTime()) || expiresAt.getTime() <= Date.now()) return;
  publishToken({ token: env.GH_APP_TOKEN, expiresAt, refreshedAt: new Date() });
}

/**
 * Set an existing token (e.g., from workflow)
 */
export function setToken(token: string, expiresInMs: number = 60 * 60 * 1000): void {
  currentToken = {
    token,
    expiresAt: new Date(Date.now() + expiresInMs),
    refreshedAt: new Date(),
  };
  console.log(`[TokenManager] Token set, expires at ${currentToken.expiresAt.toISOString()}`);
}

/**
 * Get token status for logging/debugging
 */
export function getTokenStatus(): {
  valid: boolean;
  expiresIn: number;
  needsRefresh: boolean;
  refreshedAt: Date;
} | null {
  if (!currentToken) {
    return null;
  }

  const expiresIn = Math.max(0, currentToken.expiresAt.getTime() - Date.now());

  return {
    valid: expiresIn > 0,
    expiresIn,
    needsRefresh: needsRefresh(),
    // Issue #4369: exposed so a proactive-refresh tick can tell an actual re-mint
    // from a no-op. The worker's timer used to log "Token refreshed proactively"
    // on every tick regardless, which read as proof the refresh was working while
    // the token was in fact expiring — that lie cost real diagnostic time.
    refreshedAt: currentToken.refreshedAt,
  };
}

/**
 * Execute a command with a fresh GitHub App token in the environment.
 *
 * SECURITY: Uses execFileSync with an argv array (no shell interpretation).
 * Arguments are passed directly to the process — shell metacharacters in args
 * are treated as literal text, preventing command injection.
 * See: #1149, #1163, #615/H8.
 *
 * @param file - The executable to run (e.g., "gh", "git")
 * @param args - Argument array passed directly to the process (no shell)
 * @param opts - Optional cwd and env overrides
 */
export async function execWithFreshToken(
  file: string,
  args: readonly string[],
  opts?: { cwd?: string; env?: NodeJS.ProcessEnv; retryOnAuthFailure?: boolean }
): Promise<string> {
  // Ensure we have a fresh token
  await getToken();

  const execOpts = {
    encoding: 'utf-8' as const,
    maxBuffer: 10 * 1024 * 1024,
    cwd: opts?.cwd,
    env: {
      ...process.env,
      ...opts?.env,
      GH_TOKEN: currentToken?.token,
      GITHUB_TOKEN: currentToken?.token,
    },
  };

  try {
    return execFileSync(file, [...args], execOpts).trim();
  } catch (error) {
    const err = error as { message?: string; stderr?: string };

    // Composite commands can write before a later request gets 401. Replay only
    // when the caller explicitly declares the command safe to retry.
    if (opts?.retryOnAuthFailure && (err.message?.includes('401') || err.stderr?.includes('Bad credentials'))) {
      console.log('[TokenManager] Got 401, forcing token refresh and retrying...');
      await forceRefresh();

      return execFileSync(file, [...args], {
        ...execOpts,
        env: {
          ...process.env,
          ...opts?.env,
          GH_TOKEN: currentToken?.token,
          GITHUB_TOKEN: currentToken?.token,
        },
      }).trim();
    }

    throw error;
  }
}

// ============================================================================
// CLI for testing
// ============================================================================

if (require.main === module) {
  const appId = process.env.GH_APP_ID;
  const privateKey = process.env.GH_APP_PRIVATE_KEY;
  const owner = process.env.REPO_OWNER;
  // Issue #4272: in broker mode there is no private key to require — asking for
  // one here would teach the pattern this change removes.
  const brokerMode = isBrokerEnabled();

  if (!appId || !owner || (!brokerMode && !privateKey)) {
    console.error(
      brokerMode
        ? 'Required in broker mode: GH_APP_ID, REPO_OWNER, GH_APP_INSTALLATION_ID (+ ADP_GATEWAY_ENDPOINT)'
        : 'Required: GH_APP_ID, GH_APP_PRIVATE_KEY, REPO_OWNER',
    );
    process.exit(1);
  }

  initTokenManager({
    appId,
    privateKey,
    owner,
    repo: process.env.REPO_NAME,
    installationId: process.env.GH_APP_INSTALLATION_ID,
  });

  getToken()
    .then(token => {
      console.log('Token generated successfully');
      console.log('Status:', getTokenStatus());
      // Don't print the actual token for security
    })
    .catch(err => {
      console.error('Failed to get token:', err);
      process.exit(1);
    });
}
