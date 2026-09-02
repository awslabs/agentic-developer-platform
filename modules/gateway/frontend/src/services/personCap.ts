/**
 * API client for the person-level spending limit — Issue #4629 (#4620 · C3).
 *
 * A person's own ceiling on their total agent spend, across every organization.
 * Every other budget client in this app talks to an org-scoped surface; this one
 * talks to the partition-free surface, which is what makes "my total" expressible
 * at all (#4620).
 *
 * **Only the self surface is wrapped here.** The platform-admin route
 * (`PUT /budget/person-cap/{anchor}`) exists server-side but has no client
 * function, because no screen in this app authors somebody else's personal limit
 * and adding the wrapper before the screen exists is how a target parameter ends
 * up reachable from a component that should not have one. The self functions below
 * take **no** person argument at any position — the anchor is derived from the
 * caller's token server-side, so server-side scoping is the access control and
 * there is nothing for a caller to pass.
 *
 * Snake_case wire shapes are kept verbatim (the `budgetSpend.ts` / `runStats.ts`
 * convention for `/me/*`), so the types stay diffable against
 * `src/budget/schemas.py` — a transform layer is where a field silently becomes
 * `undefined`.
 *
 * The `/api` prefix is supplied by `apiClient` and stripped again by CloudFront
 * before the origin, so the paths here are `/me/budget/person-cap`, matching the
 * router's declaration.
 */

import { apiClient, buildQueryString } from './api';
import type { BudgetPeriodType, PersonCapRequest, PersonCapResponse } from '@/types/budget';

/**
 * Read the caller's own platform-wide limit for one period.
 *
 * A backend failure raises rather than resolving to an uncapped shape: "the
 * database was unreachable" must never render as "you have no limit", so there is
 * no fallback object here and callers surface the error state.
 */
export async function getMyPersonCap(period: BudgetPeriodType): Promise<PersonCapResponse> {
  const query = buildQueryString({ period_type: period });
  return apiClient.get<PersonCapResponse>(`/me/budget/person-cap${query}`);
}

/**
 * Set the caller's own platform-wide limit for one period.
 *
 * `amountUsd` is a string at 2dp — money crosses the wire at the column's
 * precision. The server rejects `0` and negatives: removing a limit is
 * `deleteMyPersonCap`, not a `'0'`, because a zero limit and no limit are
 * different states and conflating them is how a screen shows a cap nobody set.
 */
export async function setMyPersonCap(period: BudgetPeriodType, amountUsd: string): Promise<PersonCapResponse> {
  const query = buildQueryString({ period_type: period });
  const body: PersonCapRequest = { budget_amount_usd: amountUsd };
  return apiClient.put<PersonCapResponse>(`/me/budget/person-cap${query}`, body);
}

/**
 * Remove the caller's own platform-wide limit for one period.
 *
 * Returns nothing: the endpoint answers `204` whether or not a limit existed, so a
 * retried removal is a success rather than a 404.
 */
export async function deleteMyPersonCap(period: BudgetPeriodType): Promise<void> {
  const query = buildQueryString({ period_type: period });
  await apiClient.delete<void>(`/me/budget/person-cap${query}`);
}
