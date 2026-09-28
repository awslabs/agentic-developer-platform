/**
 * The GitLab SSO handoff — one implementation, three call sites.
 *
 * `/gitlab/` is a *server*-owned path with an authenticated handoff, not a route the
 * React router owns. The backend exposes `/api/auth/gitlab-sso` (`src/auth/gitlab_sso.py`),
 * which mints an RS256 JWT and 302-redirects to `{gitlab_url}/users/auth/jwt/callback?jwt=…`.
 * That redirect is what creates the signed-in GitLab session.
 *
 * A plain `<a href="/gitlab/">` cannot carry the bearer token — it lives in
 * sessionStorage, not in a cookie — so the click handler below fetches the SSO
 * endpoint with the token and navigates to its JSON handoff destination.
 *
 * Extracted from `Navigation.tsx` (#5123). The new UI's navigation needed
 * the same behaviour, and the alternative was a second copy of it: the preview
 * rendered the GitLab entry through a react-router `Link`, which pushes client-side,
 * matches no route and lands on the `/next` catch-all 404 instead of GitLab. Copying
 * the handler would have recreated exactly the drift hazard `journeys.ts` exists to
 * remove, so both UIs now call this.
 *
 * Fails soft in every branch: any error, any unexpected status, or no token at all
 * falls back to plain `/gitlab/` navigation rather than leaving the click dead.
 */

import { getAccessToken } from './auth';

/** Where the fallback and the un-tokened default both land. */
export const GITLAB_PATH = '/gitlab/';

const SSO_ENDPOINT = '/api/auth/gitlab-sso';

/**
 * Perform the authenticated GitLab handoff.
 *
 * Returns `false` when there is no access token, meaning the caller should let the
 * anchor's own `href` navigate; returns `true` when the handoff was taken over (the
 * caller should have called `preventDefault()`).
 */
export function startGitlabSso(): boolean {
  const token = getAccessToken();
  if (!token) return false; // Let the default href navigate.

  // The authenticated backend supplies the configured GitLab callback as JSON.
  // Never follow an HTTP redirect with the gateway credential, or navigate to a
  // destination chosen by a downstream redirect from the GitLab callback.
  fetch(SSO_ENDPOINT, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    redirect: 'error',
  })
    .then(async (res) => {
      if (!res.ok || res.redirected) throw new Error('SSO handoff unavailable');
      const body: unknown = await res.json();
      if (!body || typeof body !== 'object' || !('redirect_url' in body) ||
          typeof body.redirect_url !== 'string') {
        throw new Error('Invalid SSO handoff');
      }
      const destination = new URL(body.redirect_url);
      if (!['https:', 'http:'].includes(destination.protocol) ||
          destination.username || destination.password || destination.hash ||
          !destination.pathname.endsWith('/users/auth/jwt/callback') ||
          !destination.searchParams.get('jwt')) {
        throw new Error('Invalid SSO destination');
      }
      // External GitLab origins are supported: this URL comes only from the
      // authenticated same-origin endpoint, whose authority is deployment config.
      window.location.href = destination.href;
    })
    .catch(() => {
      window.location.href = GITLAB_PATH;
    });

  return true;
}

/** `onClick` for an `<a href="/gitlab/">`: takes over unless there is no token.
 *  `preventDefault` is safe to call after `startGitlabSso` — the navigation it
 *  triggers is asynchronous, so this is still the same synchronous handler turn. */
export function handleGitlabSsoClick(event: {
  preventDefault: () => void;
}): void {
  if (startGitlabSso()) event.preventDefault();
}
