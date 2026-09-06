/**
 * API client for the person-level spending limit — Issue #4629 (#4620 · C3).
 *
 * A person's own ceiling on their total agent spend, across every organization.
 * Every other budget client in this app talks to an org-scoped surface; this one
 * talks to the partition-free surface, which is what makes "my total" expressible
 * at all (#4620).
 *
 * **Two surfaces, deliberately unlike each other.** The self functions
 * (`getMyPersonCap` / `setMyPersonCap` / `deleteMyPersonCap`) take **no** person
 * argument at any position — the anchor is derived from the caller's token
 * server-side, so server-side scoping is the access control and there is nothing
 * for a caller to pass. `setPersonCapFor` is the targeted platform-admin write and
 * necessarily does take a person; it landed in #4687, when Budget Management became
 * the first screen to author somebody else's limit. Before that this file
 * deliberately had no such wrapper, on the reasoning that a target parameter
 * reachable from a component that should not have one is how authority leaks — the
 * wrapper exists now because the screen does, not the other way round.
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

/**
 * Set **another person's** platform-wide limit. Platform admin only — Issue #4687.
 *
 * The one targeted write in this file, and the ruling on #4620 §4.2 is what makes it
 * legitimate: a platform admin already holds cross-org authority by design, so they
 * are the one party besides the person themselves who may author this row. An org
 * admin may not, not even for a member of their own org, because the row is
 * partition-free — it governs the person's spend in every other tenant they work in,
 * including tenants the org admin has no membership in.
 *
 * **The UI gate is not the access control.** `require_platform_admin` on the route is;
 * this function is callable by anyone who can reach the module, and an org admin who
 * did would get a `403`. Hiding the option in the form is an affordance that keeps
 * admins out of a dead end — it must never be mistaken for the boundary, and widening
 * the UI gate would not widen the authority, it would only produce a 403 the operator
 * has no way to interpret.
 *
 * `anchor` is `github:<numeric id>` and MUST come from a server-sourced GitHub
 * identity (`admin.getMemberGithubUserId`), never from anything the operator typed:
 * the anchor is the storage key, so a wrong one writes a cap that displays a number
 * and governs nothing (#4511). The server re-validates it against `user_identities`
 * and `422`s an unlinked id, which is the real guarantee — this note is about not
 * relying on that 422 to catch a mistake the UI should not be able to make.
 *
 * `amountUsd` is a string at 2dp for the same reason as the self path, and removing
 * somebody's limit is deliberately NOT wrapped: the admin surface authors limits, and
 * a remove affordance is a separate decision (#4687 non-goals) rather than an omission
 * to be filled in by passing `'0'` — the server rejects that, correctly.
 */
export async function setPersonCapFor(
  anchor: string,
  period: BudgetPeriodType,
  amountUsd: string
): Promise<PersonCapResponse> {
  const query = buildQueryString({ period_type: period });
  const body: PersonCapRequest = { budget_amount_usd: amountUsd };
  // `encodeURIComponent` because the anchor carries a `:` — unescaped it is a valid
  // path character, but escaping keeps the segment unambiguous if the anchor's
  // namespace ever grows a form that is not.
  return apiClient.put<PersonCapResponse>(`/budget/person-cap/${encodeURIComponent(anchor)}${query}`, body);
}
