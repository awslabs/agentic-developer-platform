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
 */

import { apiClient, buildQueryString } from './api';
import type { BudgetEnvelopeResponse, BudgetPeriodType, BudgetRunsResponse } from '@/types/budget';

/**
 * The caller's cap, settled spend and headroom for one period.
 *
 * A backend failure raises rather than returning zeroes: "the database was
 * unreachable" must never render as "$0.00 spent", so there is no fallback object
 * here and callers surface the error state.
 */
export async function getMyBudget(period: BudgetPeriodType): Promise<BudgetEnvelopeResponse> {
  const query = buildQueryString({ period });
  return apiClient.get<BudgetEnvelopeResponse>(`/me/budget${query}`);
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
  const query = buildQueryString({ period, page_size: pageSize, cursor: cursor ?? undefined });
  return apiClient.get<BudgetRunsResponse>(`/me/budget/runs${query}`);
}
