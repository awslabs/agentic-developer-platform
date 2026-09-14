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
 * endpoint with the token and navigates to the URL it resolves to.
 *
 * Extracted from `Navigation.tsx` unchanged (#5123). The new UI's navigation needed
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

  // redirect: manual would let us read a Location header, but a cross-origin 302
  // surfaces as an opaque redirect instead, so we re-request letting fetch follow it
  // and read the final URL.
  fetch(SSO_ENDPOINT, {
    headers: { Authorization: `Bearer ${token}` },
    redirect: 'manual',
  })
    .then((res) => {
      if (res.type === 'opaqueredirect') {
        return fetch(SSO_ENDPOINT, {
          headers: { Authorization: `Bearer ${token}` },
          redirect: 'follow',
        });
      }
      return res;
    })
    .then((res) => {
      if (res && res.redirected && res.url) {
        // fetch followed the 302 — go to the GitLab callback it resolved to.
        window.location.href = res.url;
      } else {
        // Unexpected 200, or the SSO endpoint is unavailable (404/503).
        window.location.href = GITLAB_PATH;
      }
    })
    .catch(() => {
      // Network error — direct navigation is better than a dead click.
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
