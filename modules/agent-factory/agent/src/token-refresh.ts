/**
 * Token Refresh Module
 *
 * Manages GitHub App installation token refresh for long-running workflows.
 * GitHub App tokens expire after 1 hour, so this module refreshes them
 * before expiration to avoid authentication failures.
 */

import { createAppAuth } from '@octokit/auth-app';
import { execSync } from 'child_process';

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
// Token Manager
// ============================================================================

let currentToken: TokenInfo | null = null;
let config: TokenManagerConfig | null = null;

// ============================================================================
// Git Remote URL Update
// ============================================================================

/**
 * Update the git remote URL with a fresh token so that git push/pull
 * operations use the new credentials instead of the stale token embedded
 * at clone time.
 */
export function updateGitRemoteToken(token: string, options?: { owner?: string; repo?: string; workDir?: string }): void {
  const owner = options?.owner || config?.owner || process.env.REPO_OWNER;
  const repo = options?.repo || config?.repo || process.env.REPO_NAME;
  const workDir = options?.workDir || config?.workDir || process.env.WORK_DIR;

  if (!owner || !repo || !workDir) {
    // Can't update git remote without knowing the repo and work directory
    return;
  }

  try {
    const url = `https://x-access-token:${token}@github.com/${owner}/${repo}.git`;
    execSync(`git remote set-url origin ${url}`, {
      cwd: workDir,
      stdio: 'ignore',
      timeout: 10000,
    });
    console.log('[TokenManager] Git remote URL updated with fresh token');
  } catch (err) {
    // Non-fatal: git remote update failure shouldn't break the token refresh flow
    console.warn(`[TokenManager] Failed to update git remote URL: ${(err as Error).message}`);
  }
}

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

    // Update environment variables so child processes use new token
    process.env.GH_TOKEN = currentToken.token;
    process.env.GITHUB_TOKEN = currentToken.token;
    process.env.GH_APP_TOKEN = currentToken.token;

    // Update git remote URL so git push/pull use the fresh token
    updateGitRemoteToken(currentToken.token);
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

  // Update git remote URL so git push/pull use the fresh token
  updateGitRemoteToken(currentToken.token);

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
 * Wrapper for execSync that refreshes token if needed
 */
export async function execWithFreshToken(command: string): Promise<string> {
  // Ensure we have a fresh token
  await getToken();

  try {
    return execSync(command, {
      encoding: 'utf-8',
      maxBuffer: 10 * 1024 * 1024,
      env: {
        ...process.env,
        GH_TOKEN: currentToken?.token,
        GITHUB_TOKEN: currentToken?.token,
      },
    }).trim();
  } catch (error) {
    const err = error as { message?: string; stderr?: string };

    // If we get a 401, try refreshing token and retrying once
    if (err.message?.includes('401') || err.stderr?.includes('Bad credentials')) {
      console.log('[TokenManager] Got 401, forcing token refresh and retrying...');
      await forceRefresh();

      return execSync(command, {
        encoding: 'utf-8',
        maxBuffer: 10 * 1024 * 1024,
        env: {
          ...process.env,
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
