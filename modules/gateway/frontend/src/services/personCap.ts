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
import type {
  BudgetPeriodType,
  PersonCapRequest,
  PersonCapResponse,
  PersonDefaultRequest,
  PersonDefaultResponse,
  PersonDefaultScope,
} from '@/types/budget';

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

/**
 * Remove **another person's** individual limit. Platform admin only — Issue #4691.
 *
 * `DELETE /budget/person-cap/{anchor}`, the route added by #4690 so an individual row
 * can be taken away and the person fall back to whatever default governs their scope.
 * The docstring above reserved this wrapper for the screen that would mount it; this
 * is that screen.
 *
 * **This is not "removing their limit" in the sense a reader expects.** Deleting the
 * individual row un-shadows the ladder underneath it: the person becomes governed by
 * their team's default, or their org's, or the platform's, and only becomes unlimited
 * if none of those exist. Copy on the calling surface has to say that, because the
 * opposite reading — "I just uncapped this person" — is both the natural one and
 * wrong wherever a default is authored.
 *
 * `204` whether or not a row existed, so a retried delete is not a failure.
 */
export async function deletePersonCapFor(anchor: string, period: BudgetPeriodType): Promise<void> {
  const query = buildQueryString({ period_type: period });
  return apiClient.delete<void>(`/budget/person-cap/${encodeURIComponent(anchor)}${query}`);
}

// ---------------------------------------------------------------------------
// DEFAULT person limits — the scope rules. Issue #4690 (D1) / #4691 (D2).
//
// Everything above is about ONE person's row. These three functions author the rules
// that govern a POPULATION: everybody in a scope with no individual row and no
// tighter-scoped rule. All are platform-admin-only server-side
// (`require_platform_admin`), which — as with `setPersonCapFor` — is the boundary;
// hiding the panel is only an affordance.
// ---------------------------------------------------------------------------

/**
 * Encode a scope as the single path segment the server parses.
 *
 * The wire forms are exactly `platform`, `org:<org_id>`, and `team:<org_id>:<team_id>`
 * (`_parse_scope`); anything else is a `422`. One segment rather than three query
 * parameters because the scope IS the identity of the resource being addressed — the
 * same reasoning `/budget/person-cap/{anchor}` uses.
 *
 * **The team form carries both ids.** A `teams.id` is unique only inside its org, so
 * a team scope naming only the team would be a rule that could govern a same-id team
 * in an unrelated tenant.
 *
 * Ids are `encodeURIComponent`-escaped individually, before the `:` separators are
 * added — escaping the assembled string would encode the separators the server splits
 * on and turn every non-platform scope into a 422.
 *
 * @throws Error when a required id is missing. A local throw, not a request: the
 *   server would reject it anyway, and a `422` from a client-side mistake surfaces
 *   through this module's fault mapping as a "backend failure" the operator is told
 *   to retry — a request that can never succeed.
 */
export function personDefaultScopePath(scope: PersonDefaultScope): string {
  if (scope.scope_type === 'platform') return 'platform';

  if (!scope.org) {
    throw new Error('A GitHub org id is required for an org- or team-scoped default person limit.');
  }
  if (scope.scope_type === 'org') return `org:${encodeURIComponent(scope.org)}`;

  if (!scope.team) {
    throw new Error('A team id is required for a team-scoped default person limit.');
  }
  return `team:${encodeURIComponent(scope.org)}:${encodeURIComponent(scope.team)}`;
}

/**
 * Read the default authored for one scope and period. Platform admin only.
 *
 * Returns the rule for THIS scope, not the rule that would apply to a member of it —
 * a team with no team-scoped rule reads `uncapped` here even while a platform default
 * governs everyone in it. See `PersonDefaultResponse`.
 *
 * A backend failure raises rather than resolving to an uncapped shape: "the table was
 * unreachable" must never render as "no default is set", which would invite an admin
 * to author a duplicate rule or believe a population is unbounded when it is not.
 */
export async function getPersonDefault(scope: PersonDefaultScope, period: BudgetPeriodType): Promise<PersonDefaultResponse> {
  const query = buildQueryString({ period_type: period });
  return apiClient.get<PersonDefaultResponse>(`/budget/person-default/${personDefaultScopePath(scope)}${query}`);
}

/**
 * Author (or re-author) the default for one scope and period. Platform admin only.
 *
 * The "$1,000/month each, unless we say otherwise" write. It governs every current
 * AND future member of the scope who has no individual row and no tighter-scoped
 * rule — which is what distinguishes it from writing the same number into every
 * person's row today, where every future joiner would start unlimited.
 *
 * Idempotent: re-authoring replaces the amount in place.
 *
 * **It takes effect within the enforcement gate's TTL, not instantly** (60s). On an
 * install whose person-limit tables were both empty, the first rule authored has to
 * wait for the process-local existence cache to expire before the person layer starts
 * consulting them at all. A surface promising immediate effect would be wrong for the
 * first minute — the one minute an operator is most likely to be testing it.
 *
 * `amountUsd` is a string at 2dp: money crosses the wire at the column's precision,
 * and a JS number would round `0.1 + 0.2`-style. The server rejects `0` — removing a
 * rule is `deletePersonDefault`.
 */
export async function setPersonDefault(scope: PersonDefaultScope, period: BudgetPeriodType, amountUsd: string): Promise<PersonDefaultResponse> {
  const query = buildQueryString({ period_type: period });
  const body: PersonDefaultRequest = { budget_amount_usd: amountUsd };
  return apiClient.put<PersonDefaultResponse>(`/budget/person-default/${personDefaultScopePath(scope)}${query}`, body);
}

/**
 * Remove one scope's default. Platform admin only.
 *
 * A DELETE, not a `PUT` of `0` — `0` is a real ceiling of zero dollars applied to
 * everybody in the scope.
 *
 * **Removing a rule does not make its members unlimited** where a broader rung still
 * covers them: deleting a team default leaves that team governed by their org's rule,
 * or the platform's. Calling surfaces must say so.
 *
 * `204` whether or not a rule existed: the outcome asked for holds either way, so a
 * retried delete is not a failure.
 */
export async function deletePersonDefault(scope: PersonDefaultScope, period: BudgetPeriodType): Promise<void> {
  const query = buildQueryString({ period_type: period });
  return apiClient.delete<void>(`/budget/person-default/${personDefaultScopePath(scope)}${query}`);
}
