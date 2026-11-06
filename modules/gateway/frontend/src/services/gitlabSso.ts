/** ADP-authenticated GitLab handoff. Errors return to the ADP launch page. */

import { getAccessToken } from './auth';

/** Where the fallback and the un-tokened default both land. */
export const GITLAB_PATH = '/gitlab-launch';

const SSO_ENDPOINT = '/api/auth/gitlab-sso';

export function validateGitlabDestination(value: string): string {
  const destination = new URL(value);
  if (destination.protocol !== 'https:' || destination.username || destination.password || destination.hash ||
      !destination.pathname.endsWith('/users/auth/jwt/callback') || !destination.searchParams.get('jwt')) {
    throw new Error('Invalid GitLab sign-in destination.');
  }
  return destination.href;
}

/** Obtain a handoff without forwarding the ADP token to GitLab. */
export async function getGitlabHandoff(): Promise<string> {
  const token = getAccessToken();
  if (!token) throw new Error('Sign into ADP to open GitLab.');
  const response = await fetch(SSO_ENDPOINT, {
    headers: { Authorization: `Bearer ${token}`, Accept: 'application/json' },
    redirect: 'error',
  });
  if (!response.ok || response.redirected) throw new Error('GitLab sign-in is unavailable. Open your Product to check its GitLab setup.');
  const body: unknown = await response.json();
  if (!body || typeof body !== 'object' || !('redirect_url' in body) || typeof body.redirect_url !== 'string') {
    throw new Error('Invalid GitLab sign-in response.');
  }
  return validateGitlabDestination(body.redirect_url);
}

export function startGitlabSso(): boolean {
  if (!getAccessToken()) return false;
  void getGitlabHandoff().then((destination) => { window.location.href = destination; })
    .catch(() => { window.location.href = '/gitlab-launch'; });
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
