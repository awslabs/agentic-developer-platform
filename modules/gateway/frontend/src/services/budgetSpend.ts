/**
 * API client for the caller's own budget read surface — Issue #4402 (U-5).
 *
 * Two endpoints, both scoped to the signed-in caller **by construction**: neither
 * accepts a `user_id` or `entity_id` parameter, so there is no parameter to abuse and
 * nothing for this module to pass. Server-side scoping is the access control; that is
 * why the screen's nav entry needs no permission gate.
 *
 * Named `budgetSpend` rather than `budget` because `services/budget.ts` already exists
 * and is a different surface: it is the admin CRUD client for `/admin/.../budgets`,
 * and it hand-transforms snake_case into camelCase. This module keeps the wire's
 * snake_case shapes verbatim (the `services/runStats.ts` convention for `/me/*`), so
 * the types stay diffable against `src/budget/schemas.py` — the transform layer is
 * where a field silently becomes `undefined`.
 *
 * The `/api` prefix is supplied by `apiClient` and stripped again by CloudFront before
 * the origin, so the paths here are `/me/budget`, matching the router's declaration.
 *
 * **The wire key for the calendar period is `period_type`, spelled exactly that way**
 * (`src/budget/me_routes.py`, both routes), and there is no alias anywhere in that
 * module. The TypeScript parameter stays `period` to match the sibling
 * `services/personCap.ts` client, so the mapping happens here at `buildQueryString`.
 * Sending `period` was defect #4970: FastAPI ignores an unknown query parameter, so
 * Daily and Weekly were served the `"monthly"` default with an HTTP 200 — a wrong
 * answer that looked like a right one. Do not rename the wire key without changing
 * the route; the response guard below is what makes a future drift visible.
 */

import { apiClient, buildQueryString } from './api';
import type { BudgetEnvelopeResponse, BudgetPeriodType, BudgetRunsResponse } from '@/types/budget';

/**
 * Refuse to return a period the caller did not ask for — Issue #4970 (D2).
 *
 * Both routes echo the period they actually resolved in `period.period_type`, derived
 * from the same argument they computed the window from. So a mismatch is always a
 * contract violation — a dropped or misspelled parameter, a proxy stripping it, a
 * response served from the wrong cache entry — and never a legitimate answer. Missing
 * metadata is treated the same way: a body that cannot say which period it describes
 * cannot be shown as a period's figures.
 *
 * This **throws**, and deliberately returns no fallback: the callers surface the
 * page's existing error affordance ("these figures are unavailable — this is not a
 * statement that your spend is zero"), which is the whole reason neither function has
 * a fallback object. Zeroing or defaulting here would recreate the defect it exists to
 * catch — a confident wrong figure instead of a visible failure.
 */
function assertPeriodMatches(requested: BudgetPeriodType, echoed: string | undefined | null, endpoint: string): void {
  if (echoed === requested) return;

  const described = echoed == null || echoed === '' ? 'no period at all' : `"${echoed}"`;
  throw new Error(
    `${endpoint} was asked for the "${requested}" period but the response describes ${described}. ` +
      'These figures are not the requested period and were not displayed.',
  );
}

/**
 * The caller's cap, settled spend and headroom for one period.
 *
 * A backend failure raises rather than returning zeroes: "the database was
 * unreachable" must never render as "$0.00 spent", so there is no fallback object
 * here and callers surface the error state.
 */
export async function getMyBudget(period: BudgetPeriodType): Promise<BudgetEnvelopeResponse> {
  const query = buildQueryString({ period_type: period });
  const response = await apiClient.get<BudgetEnvelopeResponse>(`/me/budget${query}`);
  assertPeriodMatches(period, response.period?.period_type, '/me/budget');
  return response;
}

/** Request parameters for the run drill-down. */
export interface MyBudgetRunsParams {
  period: BudgetPeriodType;
  /** Maximum runs per page (server bound: 1-100). */
  pageSize?: number;
  /** Opaque cursor from a previous response's `next_cursor`. */
  cursor?: string | null;
}

/**
 * The runs that contributed to the caller's spend in one period.
 *
 * The response's `subtotal` and `total_run_count` describe **the page**, not the
 * period — callers must label them accordingly.
 */
export async function getMyBudgetRuns({ period, pageSize, cursor }: MyBudgetRunsParams): Promise<BudgetRunsResponse> {
  const query = buildQueryString({ period_type: period, page_size: pageSize, cursor: cursor ?? undefined });
  const response = await apiClient.get<BudgetRunsResponse>(`/me/budget/runs${query}`);
  assertPeriodMatches(period, response.period?.period_type, '/me/budget/runs');
  return response;
}
