/**
 * AWS Credentials Injector — scopes AWS env vars for the agent's bash subshells.
 *
 * Problem: Pod IRSA env vars (AWS_ROLE_ARN, AWS_WEB_IDENTITY_TOKEN_FILE) are
 * inherited by child processes. When the agent spawns `aws ...` commands, they
 * silently use the platform identity instead of the user's connected role.
 *
 * Solution: Build a scoped env object that strips IRSA vars and injects the
 * user's assumed-role credentials. Pass this to the SDK's `env` option so
 * the agent's bash tool sees the right identity.
 *
 * The parent process keeps `process.env` intact — CloudWatch, S3, DynamoDB
 * clients all keep working with IRSA.
 *
 * Issue #586
 */

import { VaultGatewayClient, AssumeRoleResponse } from './complex-task-chat/vault/gateway-client';

/** Env vars that must be stripped from the agent's child-process environment. */
const IRSA_ENV_VARS = [
  'AWS_ROLE_ARN',
  'AWS_WEB_IDENTITY_TOKEN_FILE',
  'AWS_PROFILE',
] as const;

/** Env vars injected when user credentials are available. */
const AWS_CRED_ENV_VARS = [
  'AWS_ACCESS_KEY_ID',
  'AWS_SECRET_ACCESS_KEY',
  'AWS_SESSION_TOKEN',
  'AWS_REGION',
  'AWS_DEFAULT_REGION',
] as const;

/** Minimum remaining lifetime before we refresh (5 minutes). */
const REFRESH_THRESHOLD_MS = 5 * 60 * 1000;

interface CachedCreds {
  accessKeyId: string;
  secretAccessKey: string;
  sessionToken: string;
  region: string;
  expiresAt: Date;
}

export interface CredsInjector {
  /**
   * Returns an env object suitable for the SDK's `env` option.
   * Strips IRSA vars. Injects user's assumed-role creds when available.
   * Lazy-refreshes if creds are within 5 min of expiry.
   */
  getScopedEnv(): Promise<Record<string, string | undefined>>;

  /**
   * True iff the user has a connected AWS credential that was successfully
   * assumed (or will be assumed on first getScopedEnv call).
   * Used to gate the system-prompt hint.
   */
  hasCredential(): boolean;
}

export interface CredsInjectorOptions {
  /** User ID from the task envelope. */
  userId: string;
  /** Agent persona (e.g. 'developer', 'operations'). */
  agentId: string;
  /** Task ID for audit trail. */
  taskId: string;
  /** Vault gateway client instance. */
  vaultClient: VaultGatewayClient;
  /** Optional explicit credential label; omit to use the user's default AWS credential. */
  defaultLabel?: string;
  /** Optional logger. */
  log?: (msg: string) => void;
}

/**
 * Create a credentials injector scoped to a single task lifecycle.
 * Each task gets a fresh instance — no cross-task contamination.
 */
export function createCredsInjector(opts: CredsInjectorOptions): CredsInjector {
  const { userId, agentId, taskId, vaultClient, defaultLabel, log = console.log } = opts;

  let cached: CachedCreds | null = null;
  let assumeFailed = false; // true if assume-role returned no credential (user hasn't connected one)
  let initialized = false;

  async function assumeRole(): Promise<CachedCreds | null> {
    try {
      const resp: AssumeRoleResponse = await vaultClient.assumeRole({
        user_id: userId,
        agent_id: agentId,
        task_id: taskId,
        service: 'aws',
        label: defaultLabel,
        purpose: 'env-scoping for agent bash shells',
      });

      return {
        accessKeyId: resp.access_key_id,
        secretAccessKey: resp.secret_access_key,
        sessionToken: resp.session_token,
        region: resp.region,
        expiresAt: new Date(resp.expiration),
      };
    } catch (err) {
      const message = (err as Error).message ?? '';
      // 404 / "no credential" means user hasn't connected an AWS account
      if (message.includes('404') || message.includes('no_credential') || message.includes('not_found')) {
        log(`[creds-injector] No AWS credential connected for user ${userId}`);
        return null;
      }
      // Other errors (network, auth) — log and treat as no-credential
      log(`[creds-injector] Failed to assume role: ${message}`);
      return null;
    }
  }

  function isExpiringSoon(creds: CachedCreds): boolean {
    return creds.expiresAt.getTime() - Date.now() < REFRESH_THRESHOLD_MS;
  }

  /**
   * Build the base env from process.env with IRSA vars stripped.
   * This is the env the agent's bash tool will see.
   */
  function buildBaseEnv(): Record<string, string | undefined> {
    const env: Record<string, string | undefined> = { ...process.env };
    for (const key of IRSA_ENV_VARS) {
      delete env[key];
    }
    // Also strip any pre-existing AWS cred vars (defensive — shouldn't be there
    // but prevents accidental leakage from other sources)
    for (const key of AWS_CRED_ENV_VARS) {
      delete env[key];
    }
    return env;
  }

  return {
    async getScopedEnv(): Promise<Record<string, string | undefined>> {
      // First call: attempt to assume the role
      if (!initialized) {
        initialized = true;
        cached = await assumeRole();
        if (!cached) {
          assumeFailed = true;
        }
      }

      // Lazy refresh: check if creds are expiring soon
      if (cached && isExpiringSoon(cached)) {
        log('[creds-injector] Credentials expiring soon, refreshing...');
        const refreshed = await assumeRole();
        if (refreshed) {
          cached = refreshed;
        }
        // If refresh fails, keep using existing creds until they actually expire
      }

      const env = buildBaseEnv();

      // Inject user creds if available
      if (cached) {
        env.AWS_ACCESS_KEY_ID = cached.accessKeyId;
        env.AWS_SECRET_ACCESS_KEY = cached.secretAccessKey;
        env.AWS_SESSION_TOKEN = cached.sessionToken;
        env.AWS_REGION = cached.region;
        env.AWS_DEFAULT_REGION = cached.region;
      }
      // If no creds: env has IRSA stripped and no AWS identity.
      // Agent gets "Unable to locate credentials" from aws CLI — correct signal.

      return env;
    },

    hasCredential(): boolean {
      // Before initialization, we don't know yet — assume true to avoid
      // premature "no credential" messages. The actual state is resolved
      // on first getScopedEnv() call.
      if (!initialized) return true;
      return !assumeFailed;
    },
  };
}
