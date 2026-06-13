/**
 * Token Refresh Module
 *
 * Manages GitHub App installation token refresh for long-running workflows.
 * GitHub App tokens expire after 1 hour, so this module refreshes them
 * before expiration to avoid authentication failures.
 */

import { createAppAuth } from '@octokit/auth-app';
import { execFileSync } from 'child_process';
import { writeFileSync, renameSync, mkdirSync } from 'fs';
import { dirname } from 'path';

// ============================================================================
// Types
// ============================================================================

export interface TokenManagerConfig {
  appId: string;
  privateKey: string;
  installationId?: string;
  owner: string;
  repo?: string;
  workDir?: string;
  refreshThresholdMs?: number; // Refresh when this much time remains (default: 15 min)
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
 * Path to the token file read by GIT_ASKPASS and the gh wrapper at command time.
 * Using /tmp avoids accidental git-add and keeps the token out of the workspace.
 */
export const TOKEN_FILE_PATH = process.env.ADP_TOKEN_FILE || '/tmp/.adp-gh-token';

/**
 * Atomically write the current token to the file read by GIT_ASKPASS and the
 * gh wrapper. Uses write-to-temp + rename for atomicity (no partial reads).
 * File mode 0600 — readable only by the owning user.
 *
 * Non-fatal: if the write fails, the env-var fallback still works for the
 * runtime's own commands (only the SDK subprocess path degrades).
 */
export function writeTokenFile(token: string): void {
  const tmpPath = `${TOKEN_FILE_PATH}.tmp`;
  try {
    mkdirSync(dirname(TOKEN_FILE_PATH), { recursive: true, mode: 0o700 });
    writeFileSync(tmpPath, token, { mode: 0o600 });
    renameSync(tmpPath, TOKEN_FILE_PATH);
  } catch (err) {
    console.error(`[TokenManager] Failed to write token file: ${(err as Error).message}`);
  }
}

// ============================================================================
// Token Manager
// ============================================================================

let currentToken: TokenInfo | null = null;
let config: TokenManagerConfig | null = null;

/**
 * Initialize the token manager with GitHub App credentials
 */
export function initTokenManager(options: TokenManagerConfig): void {
  config = {
    ...options,
    refreshThresholdMs: options.refreshThresholdMs ?? 15 * 60 * 1000, // 15 minutes
  };
  console.log('[TokenManager] Initialized with app ID:', options.appId);
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
  if (!config?.appId || !config?.privateKey) {
    throw new Error('Token manager not configured');
  }

  console.log('[TokenManager] Generating new installation token...');

  const installationId = await getInstallationId();

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

  if (needsRefresh()) {
    currentToken = await generateNewToken();

    // Update environment variables for child processes spawned by the runtime.
    process.env.GH_TOKEN = currentToken.token;
    process.env.GITHUB_TOKEN = currentToken.token;
    process.env.GH_APP_TOKEN = currentToken.token;

    // Write to token file so SDK subprocess GIT_ASKPASS/gh-wrapper read fresh
    // tokens at command-execution time (issue #1469).
    writeTokenFile(currentToken.token);
  }

  return currentToken!.token;
}

/**
 * Force refresh the token regardless of expiry
 */
export async function forceRefresh(): Promise<string> {
  if (!config) {
    throw new Error('Token manager not initialized');
  }

  currentToken = await generateNewToken();
  process.env.GH_TOKEN = currentToken.token;
  process.env.GITHUB_TOKEN = currentToken.token;
  process.env.GH_APP_TOKEN = currentToken.token;

  // Write to token file so SDK subprocess picks up fresh token (issue #1469).
  writeTokenFile(currentToken.token);

  return currentToken.token;
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
export function getTokenStatus(): { valid: boolean; expiresIn: number; needsRefresh: boolean } | null {
  if (!currentToken) {
    return null;
  }

  const expiresIn = Math.max(0, currentToken.expiresAt.getTime() - Date.now());

  return {
    valid: expiresIn > 0,
    expiresIn,
    needsRefresh: needsRefresh(),
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
  opts?: { cwd?: string; env?: NodeJS.ProcessEnv }
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

    // If we get a 401, try refreshing token and retrying once
    if (err.message?.includes('401') || err.stderr?.includes('Bad credentials')) {
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

  if (!appId || !privateKey || !owner) {
    console.error('Required: GH_APP_ID, GH_APP_PRIVATE_KEY, REPO_OWNER');
    process.exit(1);
  }

  initTokenManager({ appId, privateKey, owner });

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
