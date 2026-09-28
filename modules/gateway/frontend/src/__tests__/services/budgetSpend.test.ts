/**
 * Tests for the budget read-surface API client — Issue #4402 (U-5).
 *
 * These go through MSW rather than a stubbed `apiClient`, so the request path and query
 * string are asserted as they would actually leave the browser. The paths matter: the
 * router declares `/me/budget` with no `/api` prefix (CloudFront strips one segment
 * before the origin) while `apiClient` prepends `/api`, so a mismatch here is a 404 that
 * a stubbed client would never reveal.
 *
 * **The wire key is asserted here on purpose (#4970).** These two tests previously
 * asserted `period`, pinning the defect: the routes declare `period_type` and no alias,
 * so the parameter was ignored and Daily/Weekly rendered the monthly default with a
 * 200. A test asserting the key the client happens to send, rather than the key the
 * route declares, cannot catch that — so both now assert `period_type` AND the absence
 * of the obsolete key. The response-period guard (D2) is covered at the end of the file.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/mocks/server';
import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';
import { mockBudgetEnvelopeFor, mockBudgetRunsFor } from '@/mocks/data/budgetSpend';

beforeEach(() => {
  sessionStorage.setItem('access_token', 'test-token');
});

afterEach(() => {
  sessionStorage.clear();
});

describe('getMyBudget', () => {
  it('requests /api/me/budget with the chosen period under the wire key `period_type`', async () => {
    // `period_type` is the ONLY key the route declares (`me_routes.py`), and it has no
    // alias — so this assertion is the contract, not a spelling preference. Sending
    // `period` was #4970: FastAPI ignored it and served the monthly default with a 200.
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetEnvelopeFor('weekly'));
      }),
    );

    const result = await getMyBudget('weekly');

    expect(seen?.pathname).toBe('/api/me/budget');
    expect(seen?.searchParams.get('period_type')).toBe('weekly');
    // The obsolete key must not come back BESIDE the new one: two spellings would let
    // the wrong one be the effective parameter again if the route ever gained an alias.
    expect(seen?.searchParams.has('period')).toBe(false);
    // The response is returned verbatim — no camelCase transform layer, which is where
    // a field silently becomes `undefined`.
    expect(result).toEqual(mockBudgetEnvelopeFor('weekly'));
  });

  it('propagates a backend failure instead of resolving to zeroes', async () => {
    // The endpoint raises 503 rather than returning zeroed figures precisely so "the
    // database was unreachable" cannot reach the screen as "$0.00 spent". The client
    // must not undo that by supplying a fallback.
    server.use(http.get('/api/me/budget', () => new HttpResponse(null, { status: 503 })));

    await expect(getMyBudget('monthly')).rejects.toThrow();
  });
});

describe('getMyBudgetRuns', () => {
  it('requests /api/me/budget/runs with the period under the wire key `period_type`', async () => {
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRunsFor('daily'));
      }),
    );

    const result = await getMyBudgetRuns({ period: 'daily' });

    expect(seen?.pathname).toBe('/api/me/budget/runs');
    expect(seen?.searchParams.get('period_type')).toBe('daily');
    expect(seen?.searchParams.has('period')).toBe(false);
    expect(result).toEqual(mockBudgetRunsFor('daily'));
  });

  it('passes page size and cursor when paginating', async () => {
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRunsFor('monthly'));
      }),
    );

    await getMyBudgetRuns({ period: 'monthly', pageSize: 50, cursor: 'cursor-2' });

    expect(seen?.searchParams.get('page_size')).toBe('50');
    expect(seen?.searchParams.get('cursor')).toBe('cursor-2');
  });

  it('omits the cursor entirely on a first page', async () => {
    // A literal `cursor=null` would be parsed as an opaque cursor by the backend rather
    // than as its absence.
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRunsFor('monthly'));
      }),
    );

    await getMyBudgetRuns({ period: 'monthly', cursor: null });

    expect(seen?.searchParams.has('cursor')).toBe(false);
  });

  it('sends no user_id or entity_id parameter', async () => {
    // The endpoints are scoped to the caller by construction and accept no target
    // parameter, so there is nothing for this client to pass and no parameter to abuse.
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRunsFor('monthly'));
      }),
    );

    await getMyBudgetRuns({ period: 'monthly' });

    expect(seen?.searchParams.has('user_id')).toBe(false);
    expect(seen?.searchParams.has('entity_id')).toBe(false);
  });
});

/**
 * The response-period guard — Issue #4970 (D2 of the approved design).
 *
 * The defect's damage was a wrong answer arriving as a success, so both clients now
 * refuse to return a body describing a period other than the one requested. The routes
 * derive the echoed `period.period_type` from the same argument they resolve the window
 * from, so a mismatch is always a contract violation — a stripped parameter, a stale
 * cached body, a future key drift — and never a legitimate answer. It cannot false-alarm.
 *
 * It must THROW rather than zero, default or fall back: the callers have no fallback
 * object precisely so a failure reaches the page's error affordance ("this is not a
 * statement that your spend is zero") instead of becoming a confident figure.
 */
describe('response-period validation', () => {
  it('rejects an envelope describing a different period than the one requested', async () => {
    // Exactly the shape the defect produced: HTTP 200, well-formed body, monthly data
    // returned to a daily request.
    server.use(http.get('/api/me/budget', () => HttpResponse.json(mockBudgetEnvelopeFor('monthly'))));

    await expect(getMyBudget('daily')).rejects.toThrow(/daily/);
  });

  it('rejects a runs page describing a different period than the one requested', async () => {
    server.use(http.get('/api/me/budget/runs', () => HttpResponse.json(mockBudgetRunsFor('monthly'))));

    await expect(getMyBudgetRuns({ period: 'weekly' })).rejects.toThrow(/weekly/);
  });

  it('rejects an envelope whose period metadata is missing entirely', async () => {
    // A body that cannot say which period it describes cannot be shown as a period's
    // figures. Treated as a violation rather than "probably what we asked for" — the
    // permissive reading is what turns absent metadata into an unnoticed wrong number.
    server.use(http.get('/api/me/budget', () => HttpResponse.json(withoutPeriodMetadata(mockBudgetEnvelopeFor('daily')))));

    await expect(getMyBudget('daily')).rejects.toThrow();
  });

  it('rejects a runs page whose period metadata is missing entirely', async () => {
    server.use(http.get('/api/me/budget/runs', () => HttpResponse.json(withoutPeriodMetadata(mockBudgetRunsFor('daily')))));

    await expect(getMyBudgetRuns({ period: 'daily' })).rejects.toThrow();
  });

  it('returns each period verbatim when the response period matches', async () => {
    // The guard must not be so eager that it rejects correct answers: all three periods
    // pass through the real handlers untouched.
    for (const period of ['daily', 'weekly', 'monthly'] as const) {
      await expect(getMyBudget(period)).resolves.toEqual(mockBudgetEnvelopeFor(period));
      await expect(getMyBudgetRuns({ period })).resolves.toEqual(mockBudgetRunsFor(period));
    }
  });
});

/**
 * A response body with its `period` metadata stripped — the "cannot say which period
 * this describes" case. Written as a deletion rather than a rest-destructure so the
 * discarded key needs no unused binding.
 */
function withoutPeriodMetadata<T extends object>(body: T): Omit<T, 'period'> {
  const copy = { ...body } as Record<string, unknown>;
  delete copy.period;
  return copy as Omit<T, 'period'>;
}
