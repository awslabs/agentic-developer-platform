/**
 * S3 fallback target resolution — shared by every fallback write path.
 *
 * Issue #4184: `AGENT_FALLBACK_BUCKET` had six readers across three files and
 * NO writer anywhere in the repo, so every read took a `|| 'adp-agent-state'`
 * branch. That bucket is not in this account (HeadBucket returns 403, and a
 * control name returns 404), and it matches no prefix in the worker's inline
 * IAM policy — confirmed `implicitDeny` via `simulate-custom-policy` against
 * the policy document alone. So every fallback write was AccessDenied, silently,
 * at the exact moment the primary path had already failed.
 *
 * Two rules encoded here, both deliberate:
 *
 * 1. **No default bucket.** A wrong default is worse than none: it turns a
 *    clear misconfiguration into a denied write that only shows up in a log
 *    nobody reads. Unset now produces one loud ERROR and an explicit skip.
 *
 * 2. **Keys are namespaced by owner/repo.** The run-logs bucket already holds
 *    objects from multiple GitHub orgs, so a key of `agent-fallback/issue-42/`
 *    collides across tenants — issue numbers are only unique within a repo.
 *    Mirrors the proven transcript key layout in
 *    agent-worker-image/entrypoint.py (`{persona}/{org}/{repo}/issue-{N}/...`).
 */

/** Env var that names the fallback bucket. Set in webhook-ingress scaledjob.tf. */
export const FALLBACK_BUCKET_ENV = 'AGENT_FALLBACK_BUCKET';

/**
 * Resolve the fallback bucket, or null when it is not configured.
 *
 * Returns null (rather than throwing or guessing) so callers skip the write
 * cleanly — these are last-resort handlers already inside a `catch`, and
 * throwing from them would lose the original error too.
 *
 * @param logError - emits the single ERROR line. Callers pass their own logger
 *                   so the message lands in the same stream as the failure
 *                   that triggered the fallback.
 */
export function resolveFallbackBucket(logError: (msg: string) => void): string | null {
  const bucket = (process.env[FALLBACK_BUCKET_ENV] || '').trim();
  if (!bucket) {
    logError(
      `${FALLBACK_BUCKET_ENV} is not set — skipping S3 fallback write. ` +
        'Output could NOT be preserved. This is a deployment misconfiguration: ' +
        'the env var is set on the agent pod template in ' +
        'modules/agent-factory/webhook-ingress/infra/scaledjob.tf (issue #4184).'
    );
    return null;
  }
  return bucket;
}

/**
 * Compose a tenant-namespaced fallback object key.
 *
 * Layout: `agent-fallback/{owner}/{repo}/issue-{n}/{timestamp}-{label}.{ext}`
 *
 * Owner/repo come from REPO_OWNER/REPO_NAME, already exported to the worker by
 * entrypoint.py. They fall back to `unknown` rather than collapsing the path
 * segment, so a missing env var cannot silently merge two tenants' output into
 * a shared prefix.
 */
export function buildFallbackKey(
  issueNumber: string | number,
  label: string,
  ext = 'md'
): string {
  const owner = (process.env.REPO_OWNER || '').trim() || 'unknown-owner';
  const repo = (process.env.REPO_NAME || '').trim() || 'unknown-repo';
  const timestamp = new Date().toISOString().replace(/[:.]/g, '-');
  return `agent-fallback/${owner}/${repo}/issue-${issueNumber}/${timestamp}-${label}.${ext}`;
}
