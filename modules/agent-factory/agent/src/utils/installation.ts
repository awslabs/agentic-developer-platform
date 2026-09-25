/** Resolve only the GitHub installation bound to this worker's target. */

export type InstallationLogger = (level: string, message: string) => void;
const GITHUB_API_ORIGIN = 'https://api.github.com';
const GITHUB_OWNER_RE = /^[A-Za-z0-9-]{1,39}$/;
const INSTALLATION_ID_RE = /^[1-9][0-9]*$/;

const defaultLog: InstallationLogger = (level, message) =>
  console.log(`[${level}] ${message}`);

export interface ResolveInstallationOptions {
  /** Target org/user. Defaults to the entrypoint's process.env.REPO_OWNER. */
  owner?: string;
  /**
   * Authoritative ID from trusted worker bootstrap, not a task/model argument.
   * Defaults to process.env.GH_APP_INSTALLATION_ID. Existing callers supply no
   * option overrides; entrypoint.py exports the run's selected installation.
   */
  installationId?: string;
  log?: InstallationLogger;
}

/**
 * Explicit bootstrap binding → owner-specific org/user lookup → refusal.
 * The App-wide installation list is never an authority source. A missing owner,
 * malformed owner, provider error or absent installation cannot select another
 * tenant's installation merely because it is first in that list.
 */
export async function resolveInstallationId(
  jwtToken: string,
  opts: ResolveInstallationOptions = {}
): Promise<string | null> {
  const log = opts.log ?? defaultLog;
  const explicit = opts.installationId ?? process.env.GH_APP_INSTALLATION_ID;
  if (explicit !== undefined) {
    if (INSTALLATION_ID_RE.test(explicit)) return explicit;
    log('WARN', 'Refusing malformed authoritative GitHub installation ID');
    return null;
  }

  const owner = opts.owner ?? process.env.REPO_OWNER;
  if (!owner || !GITHUB_OWNER_RE.test(owner)) {
    log('WARN', 'Cannot resolve installation: missing or malformed target owner');
    return null;
  }

  const authHeaders = { Authorization: `Bearer ${jwtToken}`, Accept: 'application/vnd.github+json' };
  for (const kind of ['orgs', 'users']) {
    const url = `${GITHUB_API_ORIGIN}/${kind}/${owner}/installation`;
    try {
      // Refuse a redirect before another destination receives a request. An
      // after-the-fact final-origin check cannot prevent that network access.
      const response = await fetch(url, { headers: authHeaders, redirect: 'error' });
      if (!response.ok || response.redirected || response.url !== url) continue;
      const data = await response.json() as { id?: unknown };
      if (Number.isSafeInteger(data?.id) && (data.id as number) > 0) return String(data.id);
    } catch {
      // Org and user lookup are both constrained to this exact owner. No
      // diagnostics are forwarded because transport errors may contain JWTs.
    }
  }
  log('WARN', `Could not resolve installation for owner ${owner}; refusing token mint`);
  return null;
}
