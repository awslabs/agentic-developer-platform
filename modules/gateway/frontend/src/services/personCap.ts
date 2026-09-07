/**
 * API client for the person-level spending limit — Issue #4629 (#4620 · C3).
 *
 * A person's own ceiling on their total agent spend, across every organization.
 * Every other budget client in this app talks to an org-scoped surface; this one
 * talks to the partition-free surface, which is what makes "my total" expressible
 * at all (#4620).
 *
 * **Two surfaces, deliberately unlike each other.** `getMyPersonCap` is the self
 * READ and takes **no** person argument at any position — the anchor is derived
 * from the caller's token server-side, so server-side scoping is the access
 * control and there is nothing for a caller to pass. It is the ONLY self function
 * left: the self writes (`setMyPersonCap` / `deleteMyPersonCap`) were removed with
 * their routes by the #4690 ruling — person limits are admin-governed only, so a
 * wrapper here would be a callable that can only 405. `setPersonCapFor` is the
 * targeted platform-admin write and necessarily does take a person; it landed in
 * #4687, when Budget Management became the first screen to author somebody else's
 * limit. A target parameter reachable from a component that should not have one is
 * how authority leaks — the wrapper exists because that screen does, not the other
 * way round.
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
 * Set **another person's** platform-wide limit. Platform admin only — Issue #4687.
 *
 * The one targeted write in this file, and the ruling on #4620 §4.2 is what makes it
 * legitimate: a platform admin already holds cross-org authority by design, and
 * since the #4690 ruling they are the ONLY party who may author this row — the
 * person themselves may not. An org admin may not either, not even for a member of
 * their own org, because the row is partition-free — it governs the person's spend
 * in every other tenant they work in, including tenants the org admin has no
 * membership in.
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
 * `amountUsd` is a string at 2dp — money crosses the wire at the column's
 * precision, and the server rejects `0`: removing somebody's limit is the admin
 * `DELETE /budget/person-cap/{anchor}` route (added with #4690, so an individual
 * row can fall back to the governing default), whose client wrapper arrives with
 * the admin defaults screen (#4691) that will mount it.
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
