import { apiClient } from './api';
import { getAccessToken } from './auth';

const TICKET = /^[A-Za-z0-9_.-]{16,4096}$/;

/**
 * Resolve the URL the browser must navigate to (top-level) to open the Crew dashboard.
 *
 * The Crew origin may be cross-site to this frontend (for example a `*.cloudfront.net`
 * edge, #7902), where a cookie set on a CORS fetch response is dropped by the browser.
 * So the credential exchange happens in two first-party steps: this function asks the
 * ADP origin (same-site, bearer-authenticated) for a short-lived single-use ticket, and
 * the caller then performs a real navigation to `https://<crew-origin>/crew/handoff?ticket=…`,
 * whose response sets `adp_crew` as a first-party cookie and redirects into Kiro Crew.
 * No request is ever sent from this page to the Crew origin.
 */
export async function resolveCrewDashboardDestination(handoffUrl: string): Promise<string> {
  if (!getAccessToken()) throw new Error('Sign in to open your Crew dashboard.');
  const handoff = new URL(handoffUrl);
  if (handoff.protocol !== 'https:' || handoff.username || handoff.password ||
      handoff.pathname !== '/crew/handoff' || handoff.search || handoff.hash) {
    throw new Error('Invalid Crew host');
  }
  const body: unknown = await apiClient.post('/crew/handoff-ticket', {});
  if (!body || typeof body !== 'object' || !('handoff_url' in body) ||
      typeof body.handoff_url !== 'string') throw new Error('Invalid Crew handoff');
  const destination = new URL(body.handoff_url);
  if (destination.origin !== handoff.origin || destination.pathname !== '/crew/handoff' ||
      destination.username || destination.password || destination.hash ||
      Array.from(destination.searchParams.keys()).length !== 1 ||
      !TICKET.test(destination.searchParams.get('ticket') ?? '')) {
    throw new Error('Invalid Crew destination');
  }
  return destination.href;
}

export function handleCrewDashboardClick(event: { preventDefault: () => void }, handoffUrl: string): void {
  event.preventDefault();
  if (!getAccessToken()) {
    window.alert('Sign in to open your Crew dashboard.');
    return;
  }
  void resolveCrewDashboardDestination(handoffUrl)
    .then(destination => { window.location.href = destination; })
    .catch(() => window.alert('Crew dashboard is unavailable. Try again later.'));
}
