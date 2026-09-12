import { http, HttpResponse } from 'msw';
import {
  mockBudgetEnvelopeFor,
  mockBudgetRunsFor,
  mockPersonCapEnforcing,
  mockPersonCapFor,
  mockPersonDefaultFor,
} from '../data/budgetSpend';
import { BUDGET_PERIOD_TYPES } from '@/types/budget';
import type { BudgetPeriodType } from '@/types/budget';

/**
 * Resolve the calendar period a `/me/budget*` request asked for, exactly as the routes
 * do — Issue #4970.
 *
 * The wire key is `period_type` and nothing else: `me_routes.py` declares it that way
 * on both routes and `grep 'alias='` over `src/budget/` finds none, so a request
 * carrying only `period` has NOT specified a period. Such a request resolves to the
 * route's `"monthly"` default here, which is precisely the wrong-but-successful answer
 * the defect produced — reproducing it is what lets a test detect it.
 *
 * An out-of-range value also falls back to the default rather than throwing, because
 * the route's `Literal` would have rejected it with a 422 long before any handler ran;
 * the frontend's selector can only offer the three, so this path is unreachable from
 * the UI and needs no more faithful a model than a safe default.
 */
function requestedPeriod(request: Request): BudgetPeriodType {
  const value = new URL(request.url).searchParams.get('period_type');
  return BUDGET_PERIOD_TYPES.includes(value as BudgetPeriodType) ? (value as BudgetPeriodType) : 'monthly';
}

const mockBudgets = [
  {
    id: 'budget-001',
    entity_type: 'org',
    entity_id: 'org-001',
    period_type: 'monthly',
    budget_amount_usd: 10000,
    enforcement_mode: 'soft',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
  {
    id: 'budget-002',
    entity_type: 'department',
    entity_id: 'dept-001',
    period_type: 'monthly',
    budget_amount_usd: 5000,
    enforcement_mode: 'hard',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
  {
    id: 'budget-003',
    entity_type: 'team',
    entity_id: 'team-001',
    period_type: 'monthly',
    budget_amount_usd: 1000,
    enforcement_mode: 'soft',
    org_id: 'org-001',
    updated_at: new Date().toISOString(),
  },
];

export const budgetHandlers = [
  http.get('/api/admin/organizations/:orgId/budgets', ({ params, request }) => {
    const url = new URL(request.url);
    const entityType = url.searchParams.get('entity_type');
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    let budgets = mockBudgets.filter((b) => b.org_id === params.orgId);
    if (entityType) {
      budgets = budgets.filter((b) => b.entity_type === entityType);
    }

    const start = (page - 1) * pageSize;
    const items = budgets.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: budgets.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < budgets.length,
    });
  }),

  http.get('/api/admin/organizations/:orgId/budgets/:entityType/:entityId/status', () => {
    const now = new Date();
    const periodStart = new Date(now.getFullYear(), now.getMonth(), 1);
    const periodEnd = new Date(now.getFullYear(), now.getMonth() + 1, 0);

    return HttpResponse.json({
      budget_amount_usd: 10000,
      current_spend_usd: 4523.45,
      remaining_budget_usd: 5476.55,
      budget_utilization_percent: 45.23,
      period_start: periodStart.toISOString(),
      period_end: periodEnd.toISOString(),
      period_type: 'monthly',
      enforcement_mode: 'soft',
      budget_exceeded: false,
      warnings: [],
    });
  }),

  http.post('/api/admin/organizations/:orgId/budgets', async ({ params, request }) => {
    const body = await request.json() as {
      entity_type: string;
      entity_id: string;
      period_type: string;
      budget_amount_usd: number;
      enforcement_mode?: string;
    };
    const newBudget = {
      id: `budget-${Date.now()}`,
      entity_type: body.entity_type,
      entity_id: body.entity_id,
      period_type: body.period_type,
      budget_amount_usd: body.budget_amount_usd,
      enforcement_mode: body.enforcement_mode || 'soft',
      org_id: params.orgId as string,
      updated_at: new Date().toISOString(),
    };
    return HttpResponse.json(newBudget, { status: 201 });
  }),

  http.patch('/api/admin/organizations/:orgId/budgets/:budgetId', async ({ params, request }) => {
    const budget = mockBudgets.find((b) => b.id === params.budgetId);
    if (!budget) {
      return HttpResponse.json(
        { error: 'Not found', message: 'Budget not found' },
        { status: 404 }
      );
    }
    const body = await request.json() as Record<string, unknown>;
    return HttpResponse.json({ ...budget, ...body, updated_at: new Date().toISOString() });
  }),

  // Update budget by entity type and entity ID (Issue #220)
  http.put('/api/admin/organizations/:orgId/budget/:entityType/:entityId', async ({ params, request }) => {
    const budget = mockBudgets.find(
      (b) => b.entity_type === params.entityType && b.entity_id === params.entityId
    );
    const body = await request.json() as { budget_amount_usd?: number; enforcement_mode?: string };

    const updatedBudget = budget
      ? { ...budget, ...body, updated_at: new Date().toISOString() }
      : {
          org_id: params.orgId as string,
          entity_type: params.entityType as string,
          entity_id: params.entityId as string,
          period_type: 'monthly',
          budget_amount_usd: body.budget_amount_usd || 0,
          enforcement_mode: body.enforcement_mode || 'soft',
          updated_at: new Date().toISOString(),
        };

    return HttpResponse.json(updatedBudget);
  }),

  http.delete('/api/admin/organizations/:orgId/budgets/:budgetId', () => {
    return HttpResponse.json({ success: true });
  }),

  // Delete budget by entity type, entity ID, and period type (Issue #220)
  http.delete('/api/admin/organizations/:orgId/budget/:entityType/:entityId/:periodType', () => {
    return HttpResponse.json({ success: true });
  }),

  http.get('/api/admin/organizations/:orgId/usage', ({ request }) => {
    const url = new URL(request.url);
    const page = parseInt(url.searchParams.get('page') || '1');
    const pageSize = parseInt(url.searchParams.get('page_size') || '50');

    const usageRecords = Array.from({ length: 10 }, (_, i) => ({
      id: `usage-${i + 1}`,
      entity_type: 'org',
      entity_id: 'org-001',
      period_start: new Date(Date.now() - i * 24 * 60 * 60 * 1000).toISOString().split('T')[0],
      period_type: 'daily',
      total_cost_usd: Math.random() * 500,
      total_tokens: Math.floor(Math.random() * 1000000),
      request_count: Math.floor(Math.random() * 10000),
      org_id: 'org-001',
    }));

    const start = (page - 1) * pageSize;
    const items = usageRecords.slice(start, start + pageSize);

    return HttpResponse.json({
      items,
      total: usageRecords.length,
      page,
      page_size: pageSize,
      has_more: start + pageSize < usageRecords.length,
    });
  }),

  http.get('/api/admin/organizations/:orgId/usage/timeseries', () => {
    const data = Array.from({ length: 7 }, (_, i) => ({
      timestamp: new Date(Date.now() - (6 - i) * 24 * 60 * 60 * 1000).toISOString(),
      request_count: Math.floor(Math.random() * 10000) + 1000,
      token_count: Math.floor(Math.random() * 1000000) + 100000,
      cost_usd: Math.random() * 500 + 50,
      error_count: Math.floor(Math.random() * 100),
    }));
    return HttpResponse.json(data);
  }),

  // ---------------------------------------------------------------------------
  // The caller's own budget read surface — Issue #4402 (U-5).
  //
  // Fixtures come from `mocks/data/budgetSpend.ts`, which is transcribed from
  // `src/budget/schemas.py`. See that file's header for why provenance matters here.
  //
  // **These handlers READ THE QUERY, because the routes do** — Issue #4970. They
  // previously answered with a fixed monthly body whatever was asked for, which is why
  // a client sending the period under the wrong wire key passed every test that goes
  // through MSW while showing monthly figures on the Daily and Weekly tabs in
  // production. A mock that ignores the parameter under test can only confirm that a
  // request was made, never that it was the right one.
  //
  // They model the routes' behaviour exactly, including the parts that made the defect
  // silent: the key is `period_type`, absence resolves to the `"monthly"` default, and
  // an unknown parameter (`period`, say) is IGNORED rather than rejected — so a
  // regression reproduces the real 200-with-the-wrong-body here instead of erroring.
  // Do not "fix" a failing test by relaxing this back to a canned body.
  // ---------------------------------------------------------------------------
  http.get('/api/me/budget', ({ request }) => HttpResponse.json(mockBudgetEnvelopeFor(requestedPeriod(request)))),

  http.get('/api/me/budget/runs', ({ request }) => HttpResponse.json(mockBudgetRunsFor(requestedPeriod(request)))),

  // ---------------------------------------------------------------------------
  // Person limits — Issue #4629 (#4620 · C3), narrowed to admin-governed by #4690.
  //
  // **The `/me/*` surface is READ-ONLY, and so is this mock.** `PUT` and `DELETE
  // /me/budget/person-cap` were deleted server-side by the 2026-09-07 ruling on
  // #4690 (deleted, not 403-stubbed, so the OpenAPI surface advertises no write that
  // always denies), and their handlers were removed here with them (#4691).
  //
  // Do not add them back. A mock for a route that does not exist is worse than dead
  // code: it makes a reintroduced self-service editor pass its tests in mock mode
  // against a server that can only 405 — the mock would be the sole reason the
  // feature looked like it worked. Route-absence is pinned client-side by
  // `PersonSpendingLimit.test.tsx` and server-side by
  // `test_person_cap_routes.py::TestSelfServiceWritesAreGone`.
  //
  // No `person_anchor` in the `/me/*` path: the self surface derives the person from
  // the token, so there is no target for a client to send.
  // ---------------------------------------------------------------------------
  // Period-aware for the same reason as the two reads above (#4970): this cap is the
  // DENOMINATOR beside the headline figure, it is a per-period row, and this client
  // already sent `period_type` correctly. A canned monthly cap would agree with every
  // tab, hiding whether numerator and denominator describe the same period — which is
  // the disagreement the defect put on screen (a daily limit under monthly spend).
  http.get('/api/me/budget/person-cap', ({ request }) => HttpResponse.json(mockPersonCapFor(requestedPeriod(request)))),

  // The platform-admin targeted write — Issue #4687.
  //
  // Echoes the anchor from the path and the amount from the body, so a test can tell
  // "the form sent the anchor it resolved for the person picked" from "the form sent
  // something". That distinction is the #4511 guard: a mock returning a canned anchor
  // would pass whether or not the component built the key correctly.
  //
  // ENFORCING shape, because the real route writes `hard` on every PUT (#4630) — a soft
  // echo would model a response the server cannot produce.
  http.put('/api/budget/person-cap/:anchor', async ({ params, request }) => {
    const body = (await request.json()) as { budget_amount_usd?: string };
    return HttpResponse.json({
      ...mockPersonCapEnforcing,
      person_anchor: decodeURIComponent(params.anchor as string),
      cap_usd: body.budget_amount_usd ?? mockPersonCapEnforcing.cap_usd,
    });
  }),

  // The platform-admin individual-row removal — added by #4690, mounted by #4691.
  // 204 whether or not a row existed, matching the route: a retried delete is not a
  // failure.
  http.delete('/api/budget/person-cap/:anchor', () => new HttpResponse(null, { status: 204 })),

  // ---------------------------------------------------------------------------
  // DEFAULT person limits — Issue #4690 (D1), authored by #4691 (D2).
  //
  // Per-scope only, exactly like the real API: there is no list route, so there is no
  // list handler to mock. Mocking one would let a UI be built against an endpoint
  // that does not exist.
  //
  // The scope is echoed back PARSED from the path segment, so a test can tell "the
  // component built `team:<org>:<team>` correctly" from "the component sent
  // something". A canned scope in the response would pass either way — the same
  // #4511 guard the anchor echo above exists for, applied to the scope. `platform`
  // is the seeded rung and answers `capped`; every other scope answers `uncapped`,
  // which models a fresh install where only the platform rule has been authored and
  // keeps the "No rule set" state reachable in mock mode.
  // ---------------------------------------------------------------------------
  http.get('/api/budget/person-default/:scope', ({ params, request }) => {
    const period = new URL(request.url).searchParams.get('period_type') ?? 'monthly';
    return HttpResponse.json(mockPersonDefaultFor(params.scope as string, period));
  }),

  // ENFORCING shape, because the route writes `hard` unconditionally and rejects
  // anything else — a soft echo would model a response the server cannot produce.
  http.put('/api/budget/person-default/:scope', async ({ params, request }) => {
    const body = (await request.json()) as { budget_amount_usd?: string };
    const period = new URL(request.url).searchParams.get('period_type') ?? 'monthly';
    const base = mockPersonDefaultFor(params.scope as string, period);
    return HttpResponse.json({
      ...base,
      cap_usd: body.budget_amount_usd ?? '1000.00',
      cap_status: 'capped',
      enforcement_mode: 'hard',
    });
  }),

  // 204 whether or not a rule existed, matching the route.
  http.delete('/api/budget/person-default/:scope', () => new HttpResponse(null, { status: 204 })),
];
