/**
 * Gateway base URL resolution — Issue #4146.
 *
 * The /setup page shows users an absolute gateway URL to paste into
 * `~/.claude/settings.json` and into `bg-cognito-auth.sh import`. A placeholder
 * there is worse than useless: users copy it verbatim and the failure looks like
 * a platform outage.
 *
 * Uses the deployment's runtime configuration (or local Vite settings) and
 * reuses the authoritative API-base
 * convention from `services/api.ts` (`deploymentSetting('VITE_API_URL') || '/api'`)
 * and absolutises it against the current origin — the same approach
 * `PostInstallPanel.tsx` uses to show a user an absolute URL.
 */

import { deploymentSetting } from '@/config/runtime';

/**
 * Absolute base URL of the gateway API, e.g. `https://d123.cloudfront.net/api`.
 *
 * Takes NO `/v1` suffix — Claude Code appends the API path itself.
 */
export function getGatewayBaseUrl(): string {
  const configured = deploymentSetting('VITE_API_URL') as string | undefined;

  // Already absolute (a cross-origin deployment): use it as-is.
  if (configured && /^https?:\/\//i.test(configured)) {
    return stripTrailingSlash(configured);
  }

  // Same-origin deployment (the normal CloudFront case): absolutise the path.
  const path = configured || '/api';
  const normalized = path.startsWith('/') ? path : `/${path}`;

  return stripTrailingSlash(`${window.location.origin}${normalized}`);
}

function stripTrailingSlash(url: string): string {
  return url.replace(/\/+$/, '');
}
