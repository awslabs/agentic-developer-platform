import * as fs from 'fs';
import * as path from 'path';

/**
 * Regression guard for the cross-installation 404 bug.
 *
 * refreshAppToken() used to mint a token against `installations[0]` — an
 * arbitrary (newest-first) install. Once the GitHub App is installed on more
 * than one org/user (one per onboarded tenant), installations[0] is almost
 * never the run's target org, so every comment/check-run PATCH returned 404
 * (the resource is invisible outside the token's installation).
 *
 * The fix resolves the installation for THIS run's target org via, in order:
 *   GH_APP_INSTALLATION_ID → /orgs|users/{REPO_OWNER}/installation → (last) installations[0].
 */
const SOURCE_PATH = path.join(__dirname, 'agent-worker.ts');
const source = fs.readFileSync(SOURCE_PATH, 'utf-8');

describe('agent-worker installation resolution (cross-installation 404 fix)', () => {
  it('does NOT mint the refresh token against installations[0] directly', () => {
    // The buggy line was: `/app/installations/${installations[0].id}/access_tokens`
    expect(source).not.toContain('installations[0].id}/access_tokens');
  });

  it('mints the refresh token against a resolved installation id', () => {
    expect(source).toContain('const installationId = await resolveInstallationId(jwtToken)');
    expect(source).toContain('/app/installations/${installationId}/access_tokens');
  });

  it('defines a resolveInstallationId helper', () => {
    expect(source).toMatch(/async function resolveInstallationId\(jwtToken: string\)/);
  });

  it('prefers the explicit GH_APP_INSTALLATION_ID env var first', () => {
    const fn = source.slice(source.indexOf('async function resolveInstallationId'));
    const explicitIdx = fn.indexOf('GH_APP_INSTALLATION_ID');
    const ownerIdx = fn.indexOf('REPO_OWNER');
    const fallbackIdx = fn.indexOf('/app/installations');
    expect(explicitIdx).toBeGreaterThan(-1);
    // explicit id resolved before the owner lookup, which is before the installations[0] fallback
    expect(explicitIdx).toBeLessThan(ownerIdx);
    expect(ownerIdx).toBeLessThan(fallbackIdx);
  });

  it('resolves by REPO_OWNER via both org and user installation endpoints', () => {
    expect(source).toContain('${kind}/${owner}/installation');
    expect(source).toContain("for (const kind of ['orgs', 'users'])");
  });

  it('only falls back to installations[0] as a last resort, with a warning', () => {
    expect(source).toContain('last resort');
    expect(source).toMatch(/installations\[0\]\.id/); // still present, but only in the guarded fallback
  });

  it('passes installationId through to the TokenManager too', () => {
    expect(source).toContain('installationId: process.env.GH_APP_INSTALLATION_ID');
  });
});
