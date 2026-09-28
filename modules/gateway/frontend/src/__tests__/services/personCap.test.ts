/**
 * Tests for the targeted person-cap write — Issue #4687.
 *
 * `setPersonCapFor` is the one function in `personCap.ts` that takes a person, so it is
 * the one that can address the wrong one. What matters is the request it builds: the
 * anchor lands in the path (it is the storage key, so a mangled segment writes a cap
 * that displays a number and governs nothing — #4511), the period is a query parameter
 * rather than part of the body, and the amount crosses the wire as a string at the
 * column's precision.
 *
 * The self functions are exercised through PersonSpendingLimit.test.tsx.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { apiClient } from '@/services/api';
import { setPersonCapFor } from '@/services/personCap';

vi.mock('@/services/api', async (importOriginal) => ({
  // The REAL buildQueryString, not a re-implementation (review fix on #4688,
  // matching activity.test.ts): a verbatim copy keeps this suite green against
  // stale URL semantics if the real helper's encoding ever changes — evaporating
  // exactly the request-line pinning this file exists for.
  ...(await importOriginal<typeof import('@/services/api')>()),
  apiClient: {
    get: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}));

describe('setPersonCapFor (#4687)', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(apiClient.put).mockResolvedValue({
      person_anchor: 'github:20402445',
      period_type: 'monthly',
      cap_usd: '500.00',
      enforcement_mode: 'hard',
    });
  });

  it('PUTs to the targeted admin route with the anchor in the path', async () => {
    await setPersonCapFor('github:20402445', 'monthly', '500.00');

    expect(apiClient.put).toHaveBeenCalledWith(
      '/budget/person-cap/github%3A20402445?period_type=monthly',
      { budget_amount_usd: '500.00' }
    );
  });

  it('does not use the self surface', async () => {
    // `/me/budget/person-cap` derives the person from the token, so sending an admin's
    // intended target there would cap the ADMIN — a silent mis-address rather than an
    // error, since that request is perfectly valid.
    await setPersonCapFor('github:20402445', 'monthly', '500.00');

    const [path] = vi.mocked(apiClient.put).mock.calls[0];
    expect(path).not.toContain('/me/');
  });

  it('carries no `/api` prefix — apiClient supplies it', async () => {
    // The router declares `/budget/person-cap/{anchor}`; CloudFront strips the first
    // `/api` before the origin. A second one here would miss the mount.
    await setPersonCapFor('github:20402445', 'monthly', '500.00');

    const [path] = vi.mocked(apiClient.put).mock.calls[0];
    expect(path.startsWith('/budget/')).toBe(true);
  });

  it('sends the amount as a string, not a number', async () => {
    // Money at the column's precision. A JS number would round-trip through a float.
    await setPersonCapFor('github:20402445', 'monthly', '1234.50');

    const [, body] = vi.mocked(apiClient.put).mock.calls[0];
    expect(body).toEqual({ budget_amount_usd: '1234.50' });
    expect(typeof (body as { budget_amount_usd: unknown }).budget_amount_usd).toBe('string');
  });

  it('sends the period as a query parameter, one row per period', async () => {
    await setPersonCapFor('github:20402445', 'daily', '50.00');

    expect(apiClient.put).toHaveBeenCalledWith(
      '/budget/person-cap/github%3A20402445?period_type=daily',
      { budget_amount_usd: '50.00' }
    );
  });

  it('sets no enforcement mode — the server always writes hard', async () => {
    // #4630: authoring the limit IS the choice to be enforced, and the route ignores any
    // client preference. Sending one would imply a choice the caller does not have.
    await setPersonCapFor('github:20402445', 'monthly', '500.00');

    const [, body] = vi.mocked(apiClient.put).mock.calls[0];
    expect(body).not.toHaveProperty('enforcement_mode');
  });

  it('propagates a 403 rather than swallowing it', async () => {
    // The UI hides this option from org admins, but the route's `require_platform_admin`
    // is the boundary. If it ever answers, the caller must see it.
    vi.mocked(apiClient.put).mockRejectedValue(new Error('Forbidden'));

    await expect(setPersonCapFor('github:20402445', 'monthly', '500.00')).rejects.toThrow(
      'Forbidden'
    );
  });

  it('returns the server response unchanged', async () => {
    // Snake_case kept verbatim, so the type stays diffable against schemas.py.
    const result = await setPersonCapFor('github:20402445', 'monthly', '500.00');

    expect(result).toEqual({
      person_anchor: 'github:20402445',
      period_type: 'monthly',
      cap_usd: '500.00',
      enforcement_mode: 'hard',
    });
  });
});
