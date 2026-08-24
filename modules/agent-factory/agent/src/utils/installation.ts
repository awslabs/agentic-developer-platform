/**
 * Shared GitHub App installation resolution.
 *
 * A GitHub App installed on more than one org/user (one installation per
 * onboarded tenant) exposes every installation via `GET /app/installations`,
 * newest-first. Minting a token against `installations[0]` therefore picks an
 * arbitrary tenant — not the one this run is authorized for. That token is
 * cross-tenant: it can read and write repositories belonging to a different
 * customer, and every API call against the *intended* repo returns 404 because
 * the resource is invisible outside the token's own installation.
 *
 * This module owns the one correct resolution ladder so that every caller binds
 * to the run's own installation. It deliberately has no module-level state and
 * no side effects at import time, so it is safe to import from library code
 * (see `utils/ghPost.ts`, which is imported by four entrypoints).
 */

/** Minimal logger shape — matches the `log(level, message)` convention in-tree. */
export type InstallationLogger = (level: string, message: string) => void;

const defaultLog: InstallationLogger = (level, message) =>
  console.log(`[${level}] ${message}`);

export interface ResolveInstallationOptions {
  /** Target org/user. Defaults to `process.env.REPO_OWNER`. */
  owner?: string;
  /** Authoritative installation id. Defaults to `process.env.GH_APP_INSTALLATION_ID`. */
  installationId?: string;
  /** Where warnings go. Defaults to `console.log`. */
  log?: InstallationLogger;
}

/**
 * Resolve the GitHub App installation id for this run's target org.
 *
 * Resolution order:
 *   1. GH_APP_INSTALLATION_ID — authoritative, exported by entrypoint.py for the
 *      exact installation that received the triggering webhook.
 *   2. /orgs/{owner}/installation then /users/{owner}/installation —
 *      resolve by the target owner via the App JWT.
 *   3. Last resort: installations[0] (with a warning) — preserves old behavior
 *      only when no owner/installation context is available at all.
 *
 * @returns the installation id as a string, or null if none could be resolved.
 */
export async function resolveInstallationId(
  jwtToken: string,
  opts: ResolveInstallationOptions = {}
): Promise<string | null> {
  const log = opts.log ?? defaultLog;

  const explicit = opts.installationId ?? process.env.GH_APP_INSTALLATION_ID;
  if (explicit) return explicit;

  const owner = opts.owner ?? process.env.REPO_OWNER;
  const authHeaders = { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' };

  if (owner) {
    for (const kind of ['orgs', 'users']) {
      try {
        const r = await fetch(`https://api.github.com/${kind}/${owner}/installation`, { headers: authHeaders });
        if (r.ok) {
          const data = await r.json() as { id: number };
          if (data?.id) return String(data.id);
        }
      } catch {
        // try next kind
      }
    }
    log('WARN', `Could not resolve installation for owner ${owner}; falling back to installations[0]`);
  }

  // Last-resort fallback (legacy behavior) — only when no target context exists.
  const resp = await fetch('https://api.github.com/app/installations', { headers: authHeaders });
  const installations = await resp.json() as Array<{ id: number }>;
  if (!installations.length) return null;
  log('WARN', 'Using installations[0] as a last resort — REPO_OWNER/GH_APP_INSTALLATION_ID not set');
  return String(installations[0].id);
}
