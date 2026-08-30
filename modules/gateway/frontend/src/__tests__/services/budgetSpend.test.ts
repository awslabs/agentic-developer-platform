/**
 * Tests for the budget read-surface API client — Issue #4402 (U-5).
 *
 * These go through MSW rather than a stubbed `apiClient`, so the request path and query
 * string are asserted as they would actually leave the browser. The paths matter: the
 * router declares `/me/budget` with no `/api` prefix (CloudFront strips one segment
 * before the origin) while `apiClient` prepends `/api`, so a mismatch here is a 404 that
 * a stubbed client would never reveal.
 */

import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '@/mocks/server';
import { getMyBudget, getMyBudgetRuns } from '@/services/budgetSpend';
import { mockBudgetEnvelope, mockBudgetRuns } from '@/mocks/data/budgetSpend';

beforeEach(() => {
  sessionStorage.setItem('access_token', 'test-token');
});

afterEach(() => {
  sessionStorage.clear();
});

describe('getMyBudget', () => {
  it('requests /api/me/budget with the chosen period', async () => {
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetEnvelope);
      }),
    );

    const result = await getMyBudget('weekly');

    expect(seen?.pathname).toBe('/api/me/budget');
    expect(seen?.searchParams.get('period')).toBe('weekly');
    // The response is returned verbatim — no camelCase transform layer, which is where
    // a field silently becomes `undefined`.
    expect(result).toEqual(mockBudgetEnvelope);
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
  it('requests /api/me/budget/runs with the period', async () => {
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRuns);
      }),
    );

    const result = await getMyBudgetRuns({ period: 'daily' });

    expect(seen?.pathname).toBe('/api/me/budget/runs');
    expect(seen?.searchParams.get('period')).toBe('daily');
    expect(result).toEqual(mockBudgetRuns);
  });

  it('passes page size and cursor when paginating', async () => {
    let seen: URL | undefined;
    server.use(
      http.get('/api/me/budget/runs', ({ request }) => {
        seen = new URL(request.url);
        return HttpResponse.json(mockBudgetRuns);
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
        return HttpResponse.json(mockBudgetRuns);
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
        return HttpResponse.json(mockBudgetRuns);
      }),
    );

    await getMyBudgetRuns({ period: 'monthly' });

    expect(seen?.searchParams.has('user_id')).toBe(false);
    expect(seen?.searchParams.has('entity_id')).toBe(false);
  });
});
