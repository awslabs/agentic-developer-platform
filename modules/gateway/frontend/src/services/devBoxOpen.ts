/**
 * Opening the Crew dashboard in a new tab without a pop-up blocker in the way (#8223 UX).
 *
 * Browsers allow `window.open` only while the user's click is being handled. The earlier
 * flow resolved the handoff ticket first (several seconds of polling) and opened the tab
 * afterwards, so every browser treated it as an unsolicited pop-up. The flow is now:
 *
 *   1. In the click handler, synchronously, the page records an *open intent* and opens
 *      `DEV_BOX_OPEN_PATH?intent=<nonce>` — an ADP route, never a blank tab.
 *   2. That interstitial (`pages/settings/DevBoxOpen`) runs the one-click flow
 *      (`useDevBox().openBox`): ensure/start, enroll, wait for Crew, mint the ticket, then
 *      `location.replace(handoff_url)` — a top-level navigation on the Crew origin, which is
 *      what lets it set the first-party `adp_crew` cookie.
 *   3. ADP stays open in the first tab, so "coming back" is a tab switch.
 *
 * The nonce is a one-time, two-minute record in localStorage (shared between same-origin
 * tabs; sessionStorage is not copied into a tab opened with `noopener`). Without a matching
 * nonce the interstitial connects only (`ensure: false`): it never provisions, starts or
 * enrolls. That keeps a cross-site navigation to this URL from starting the owner's box.
 *
 * Why `window.open(path, '_blank')` without `noopener` here: per the HTML spec, `noopener`
 * makes `window.open` return `null` even when the tab opened, which is indistinguishable
 * from a blocked pop-up; the old blocked-tab detection tripped on exactly that. The target is
 * our own origin, so an opener reference is harmless.
 */

export const DEV_BOX_OPEN_PATH = '/settings/dev-box/open';
const INTENT_KEY = 'adp.devbox.open-intent';
const INTENT_MAX_AGE_MS = 2 * 60_000;
const NONCE = /^[0-9a-f-]{36}$/;

/** Record the owner's click and return the interstitial href carrying its one-time nonce. */
export function beginDevBoxOpen(): string {
  const nonce = crypto.randomUUID();
  try {
    localStorage.setItem(INTENT_KEY, JSON.stringify({ nonce, at: Date.now() }));
  } catch {
    // Storage unavailable: the interstitial then connects only and says so.
  }
  return `${DEV_BOX_OPEN_PATH}?intent=${nonce}`;
}

/**
 * Consume the click intent: true when `nonce` matches the fresh record written by
 * `beginDevBoxOpen` in this browser. Always removes the record, so it is redeemable once.
 */
export function consumeDevBoxOpenIntent(nonce: string | null): boolean {
  if (!nonce || !NONCE.test(nonce)) return false;
  let raw: string | null;
  try {
    raw = localStorage.getItem(INTENT_KEY);
    if (raw !== null) localStorage.removeItem(INTENT_KEY);
  } catch {
    return false;
  }
  if (!raw) return false;
  try {
    const record: unknown = JSON.parse(raw);
    return !!record && typeof record === 'object'
      && (record as { nonce?: unknown }).nonce === nonce
      && typeof (record as { at?: unknown }).at === 'number'
      && Date.now() - (record as { at: number }).at < INTENT_MAX_AGE_MS;
  } catch {
    return false;
  }
}

/**
 * Open the interstitial synchronously from a click. Returns the tab, or `null` with the href
 * the page should offer as a visible "Open in new tab" link when the browser still refused.
 */
export function openDevBoxTab(): { tab: Window | null; href: string } {
  const href = beginDevBoxOpen();
  const tab = window.open(href, '_blank');
  return { tab, href };
}
