/**
 * services/devBoxOpen — the click-time intent that lets the interstitial tab provision or
 * start the box, and the synchronous tab open that no pop-up blocker sees (#8223 UX).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { DEV_BOX_OPEN_PATH, beginDevBoxOpen, consumeDevBoxOpenIntent, openDevBoxTab } from '@/services/devBoxOpen';

beforeEach(() => { localStorage.clear(); vi.restoreAllMocks(); });
afterEach(() => { vi.useRealTimers(); });

const nonceOf = (href: string) => new URL(href, 'https://adp.example.test').searchParams.get('intent');

describe('open intent', () => {
  it('is redeemable exactly once, only with the nonce it was issued with', () => {
    const href = beginDevBoxOpen();
    expect(href.startsWith(`${DEV_BOX_OPEN_PATH}?intent=`)).toBe(true);
    const nonce = nonceOf(href)!;
    expect(nonce).toMatch(/^[0-9a-f-]{36}$/);
    expect(consumeDevBoxOpenIntent('00000000-0000-4000-8000-000000000000')).toBe(false);
    // A wrong nonce still consumed the record: nothing is left for a later guess.
    expect(consumeDevBoxOpenIntent(nonce)).toBe(false);
    const again = nonceOf(beginDevBoxOpen())!;
    expect(consumeDevBoxOpenIntent(again)).toBe(true);
    expect(consumeDevBoxOpenIntent(again)).toBe(false);
  });

  it('refuses a missing, malformed or stale nonce, so a plain navigation to the URL cannot start a box', () => {
    expect(consumeDevBoxOpenIntent(null)).toBe(false);
    expect(consumeDevBoxOpenIntent('')).toBe(false);
    expect(consumeDevBoxOpenIntent('<script>')).toBe(false);
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-20T06:00:00Z'));
    const nonce = nonceOf(beginDevBoxOpen())!;
    vi.setSystemTime(new Date('2026-10-20T06:02:01Z'));
    expect(consumeDevBoxOpenIntent(nonce)).toBe(false);
  });

  it('ignores a corrupt record', () => {
    localStorage.setItem('adp.devbox.open-intent', '{not json');
    expect(consumeDevBoxOpenIntent('00000000-0000-4000-8000-000000000000')).toBe(false);
    localStorage.setItem('adp.devbox.open-intent', JSON.stringify({ nonce: 'x', at: 'now' }));
    expect(consumeDevBoxOpenIntent('00000000-0000-4000-8000-000000000000')).toBe(false);
  });
});

describe('openDevBoxTab', () => {
  it('opens the interstitial href synchronously, same-origin and without noopener (noopener would return null even on success)', () => {
    const tab = {} as Window;
    const open = vi.spyOn(window, 'open').mockReturnValue(tab);
    const result = openDevBoxTab();
    expect(open).toHaveBeenCalledExactlyOnceWith(result.href, '_blank');
    expect(result.tab).toBe(tab);
    expect(result.href.startsWith(`${DEV_BOX_OPEN_PATH}?intent=`)).toBe(true);
    expect(consumeDevBoxOpenIntent(nonceOf(result.href))).toBe(true);
  });

  it('returns the href for a visible link when the browser refuses even a click-time open', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    const result = openDevBoxTab();
    expect(result.tab).toBeNull();
    expect(nonceOf(result.href)).toMatch(/^[0-9a-f-]{36}$/);
  });
});
