import * as jwt from 'jsonwebtoken';
import { execSync } from 'child_process';
import { ConfigLoader } from './ConfigLoader';
import { Logger } from './Logger';
import { GitHubAppCredentials } from '../types';

interface PatCredentials {
  token: string;
}

export class TokenManager {
  private configLoader: ConfigLoader;
  private logger: Logger;
  private credentials: GitHubAppCredentials | null = null;
  private patToken: string | null = null;
  private installationToken: string | null = null;
  private tokenExpiry: Date | null = null;
  private refreshTimer: NodeJS.Timeout | null = null;
  private usePatMode: boolean = false;

  constructor(configLoader: ConfigLoader, logger: Logger) {
    this.configLoader = configLoader;
    this.logger = logger;
  }

  async initialize(): Promise<void> {
    // Try PAT first, fall back to GitHub App
    try {
      const patCreds = await this.configLoader.getSecret<PatCredentials>('github-pat');
      if (patCreds.token) {
        this.patToken = patCreds.token;
        this.usePatMode = true;
        this.logger.info('TokenManager initialized with PAT', { component: 'TokenManager' });
        return;
      }
    } catch {
      // PAT not found, try GitHub App credentials
    }

    this.credentials = await this.configLoader.getSecret<GitHubAppCredentials>('app-credentials');
    await this.refreshToken();
    this.startRefreshTimer();
    this.logger.info('TokenManager initialized with GitHub App', { component: 'TokenManager' });
  }

  private async refreshToken(): Promise<void> {
    if (this.usePatMode) return;
    if (!this.credentials) throw new Error('Credentials not loaded');

    const now = Math.floor(Date.now() / 1000);
    const jwtToken = jwt.sign(
      { iat: now - 60, exp: now + 600, iss: this.credentials.appId },
      this.credentials.privateKey,
      { algorithm: 'RS256' }
    );

    const response = await fetch(
      `https://api.github.com/app/installations/${this.credentials.installationId}/access_tokens`,
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${jwtToken}`,
          Accept: 'application/vnd.github+json',
        },
      }
    );

    if (!response.ok) {
      throw new Error(`Failed to get installation token: ${response.status}`);
    }

    const data = await response.json() as { token: string; expires_at: string };
    this.installationToken = data.token;
    this.tokenExpiry = new Date(data.expires_at);
    // Update environment so gh CLI and child processes use the fresh token
    process.env.GH_TOKEN = data.token;
    process.env.GITHUB_TOKEN = data.token;
    process.env.GH_APP_TOKEN = data.token;

    // Update git remote URL so git push/pull use the fresh token
    this.updateGitRemoteUrl(data.token);

    this.logger.debug('Token refreshed', { component: 'TokenManager', expiry: data.expires_at });
  }

  startRefreshTimer(): void {
    if (this.usePatMode) return;
    this.refreshTimer = setInterval(async () => {
      if (this.tokenExpiry && Date.now() > this.tokenExpiry.getTime() - 5 * 60 * 1000) {
        await this.refreshToken();
      }
    }, 60 * 1000);
  }

  stopRefreshTimer(): void {
    if (this.refreshTimer) {
      clearInterval(this.refreshTimer);
      this.refreshTimer = null;
    }
  }

  /**
   * Update the git remote URL with a fresh token so git push/pull
   * operations use new credentials instead of the stale clone-time token.
   */
  private updateGitRemoteUrl(token: string): void {
    const owner = process.env.REPO_OWNER;
    const repo = process.env.REPO_NAME;
    const workDir = process.env.WORK_DIR;

    if (!owner || !repo || !workDir) {
      return;
    }

    try {
      const url = `https://x-access-token:${token}@github.com/${owner}/${repo}.git`;
      execSync(`git remote set-url origin ${url}`, {
        cwd: workDir,
        stdio: 'ignore',
        timeout: 10000,
      });
      this.logger.debug('Git remote URL updated with fresh token', { component: 'TokenManager' });
    } catch (err) {
      // Non-fatal: don't let git remote update failure break token refresh
      this.logger.warn('Failed to update git remote URL', {
        component: 'TokenManager',
        error: (err as Error).message,
      });
    }
  }

  async getToken(): Promise<string> {
    if (this.usePatMode && this.patToken) {
      return this.patToken;
    }
    if (!this.installationToken || !this.tokenExpiry || Date.now() > this.tokenExpiry.getTime() - 60 * 1000) {
      await this.refreshToken();
    }
    return this.installationToken!;
  }
}
