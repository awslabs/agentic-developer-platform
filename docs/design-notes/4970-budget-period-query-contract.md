# Design Note: One Period Contract for the Budget & Spend Read Path (Issue #4970)

> **Status**: Design — awaiting issue-owner approval before implementation
> **Author**: @agent-architect
> **Date**: 2026-09-12
> **Issue**: #4970 — Daily and Weekly tabs display Monthly spend and runs
> **Mode**: Per-issue design review (defect)
> **Verdict**: Ready to implement as specified, with three decisions for the owner (§6)
> **Audited tree**: `662906e` (`main`), the commit the issue names
> **Related**: #4324 (visibility EPIC), #4397 / #4400 (the two `/me/budget*` APIs),
> #4402 / #4669 / #4685 / #4686 (the page), #4629 (personal limit), #4969 (pricing — out of scope)

---

## 0. Summary

The defect reproduces in the current tree and is two lines wide.
`modules/gateway/frontend/src/services/budgetSpend.ts:31` and `:51` put the selected
period on the wire under the key `period`. Both routes declare it as `period_type`
(`modules/gateway/src/budget/me_routes.py:875` for `/me/budget`, `:1282` for
`/me/budget/runs`), with a default of `"monthly"` and no alias — `grep 'alias='` over
`src/budget/` and `src/activity/` finds none anywhere in the module. FastAPI ignores the
unknown parameter and serves the default, so Daily and Weekly return a monthly envelope
with HTTP 200. The sibling client for the personal limit already sends the right key
(`services/personCap.ts:50`), which is why a daily limit can appear beside monthly spend.

The recommendation is the narrow fix: send `period_type` from both functions, keep the
TypeScript parameter named `period` (the `personCap.ts` shape), and change nothing
server-side. Everything else in this note is about making the fix *provable* — the
current tests pass on the broken code and would pass again on the fixed code, which is
the reason this shipped.

Two findings adjust the issue's own plan and both reduce scope:

1. **No period-consistency plumbing is needed beyond the two keys.** Spend, dates,
   breakdowns, the limit denominator and the runs list all derive from one `useState`
   period (`pages/BudgetSpend.tsx:127`) that is both the query key and the fetch
   argument on every path, including the limit's shared hook (`hooks/usePersonCap.ts:21`).
   Once the wire key is right, the periods cannot disagree. §3 walks each surface.
2. **There is no run pagination to scope a cursor to.** `MySpend` renders
   `<BudgetRunsTable data={runs} .../>` with no `onLoadMore`
   (`components/budget/SpendTiles.tsx:258`), and the table only draws "Load more runs"
   when that prop is present (`components/budget/BudgetRunsTable.tsx:160`). No cursor
   state exists in the page. Building it here would be a new feature, not this fix (§4.4).

---

## 1. The canonical contract

`period_type`, spelled that way, on every calendar-period read in this surface.
It is what all four routes already declare and what the backend tests pin:

| Surface | Route declaration | Client | Sends today |
|---|---|---|---|
| Own budget envelope | `me_routes.py:875` | `services/budgetSpend.ts:31` | `period` ❌ |
| Own contributing runs | `me_routes.py:1282` | `services/budgetSpend.ts:51` | `period` ❌ |
| Own personal limit | `person_cap` route | `services/personCap.ts:50` | `period_type` ✅ |
| Default person limits | — | `services/personCap.ts` (defaults) | `period_type` ✅ |
| Managed (operator) scope | `managed_scope_routes.py:562`, `:639` | no client exists | n/a |

Accepted values are `daily` / `weekly` / `monthly`, enforced twice server-side: the
route's `Literal` gives a 422 at the boundary, and `_resolve_period_bounds`
(`me_routes.py:169`) gives a 422 for anything non-calendar that gets past it. The
frontend's `BUDGET_PERIOD_TYPES` (`types/budget.ts:99`) is the same three values, so the
selector cannot offer a query that must fail. None of that changes.

`/me/budget/runs` also accepts `period_start`, `page_size` and `cursor`
(`me_routes.py:1286-1290`). The client sends `page_size` and `cursor` correctly today and
never sends `period_start`, which is right: omitting it means "the current period", and
the route normalises any day inside a period to that period's real bounds.

**The parameter name in TypeScript stays `period`.** `getMyPersonCap(period)` already
takes a UI-vocabulary `period` and maps it to the wire's `period_type` at the call to
`buildQueryString`. Matching that keeps the diff to the two mapping expressions and keeps
one convention across the three `/me/*` budget clients.

---

## 2. Backend compatibility: recommend no change

Two options were considered for accepting `period` server-side.

**Option A — add `alias="period"` (or a second accepted key) to both routes.** Rejected.
It permanently widens a public API contract to accommodate one client's bug, in a module
that uses no aliases anywhere, and it leaves two spellings for one concept in the OpenAPI
surface for every future client to choose wrongly between. It also cannot be un-shipped
cheaply once a client depends on it.

**Option B — reject an unknown `period` with a 422.** Rejected. FastAPI ignores unknown
query parameters by design across this whole application; making one route strict is a
new convention for no benefit here, and it would turn a stale cached bundle into a hard
error page instead of the (wrong but familiar) monthly view.

The one real argument for Option A is the deploy window: a browser holding the old bundle
keeps sending `period` until it reloads. Without an alias that browser keeps seeing what
it sees today — monthly data on all three tabs. That is not a regression, it is the
status quo for the minutes until the CloudFront invalidation and a reload land, and the
frontend deploy invalidates `/*` (`gateway-deploy.yml`, "Deploy to S3 and invalidate
CloudFront"). **Recommendation: fix the client only.** Decision D1 in §6.

### 2.1 Response-period validation (recommended, small)

The defect's damage came from a wrong answer arriving as a success. The server echoes the
resolved period in `period.period_type` on both responses (`me_routes.py:1030`, `:1336`),
so the client can cheaply refuse to render a period it did not ask for: in each service
function, compare `response.period.period_type` to the requested period and throw when
they differ.

This is worth the four lines because it converts the *entire class* of defect — any future
key drift, a proxy stripping a parameter, a cached response served under the wrong key —
from "confident wrong number" into the page's existing error state, which already says
"these figures are unavailable — this is not a statement that your spend is zero"
(`pages/BudgetSpend.tsx:162`). It cannot produce a false alarm, because a mismatch is
always a contract violation: the route derives the echoed value from the same argument it
resolved the window from.

It must throw, not zero, not fall back: the raise path is the reason `getMyBudget` has no
fallback object today (`services/budgetSpend.ts:27-30`). Decision D2 in §6.

---

## 3. Selected-period consistency, surface by surface

Traced against the current tree, with the two keys fixed:

| Surface | Source of its period | Consistent after the fix? |
|---|---|---|
| Headline spend | `envelope.person_envelope.spend_usd` (`SpendTiles.tsx:166`) | Yes — envelope fetched with the selected period |
| Date window / resets-in | `envelope.period.*` (`BudgetSpend.tsx:194-198`) | Yes — same response |
| Direct-use & other capped lines | `envelope.lines` (`SpendTiles.tsx:237`) | Yes — same response |
| Per-workspace breakdown | `envelope.per_org` (`SpendTiles.tsx:224`) | Yes — same response |
| Personal-limit denominator | `usePersonCap(period)` (`SpendTiles.tsx:164`) | Yes — already correct; now paired with a matching numerator |
| Agent runs drill-down | `getMyBudgetRuns({ period })` (`SpendTiles.tsx:180`) | Yes — second key fixed |
| Caption wording ("per day/week/month") | the `period` prop (`SpendTiles.tsx:142`) | Yes — already the selected value |

**Cached responses and rapid tab changes.** All three queries are keyed by period —
`['myBudget', period]`, `['myBudgetRuns', period]`, `personCapQueryKey(period)`. Switching
tabs switches cache entry, so a period's data is never served under another period's key.
React Query returns `undefined` for a key it has never fetched, so a fast Daily→Weekly→Daily
sequence resolves each into its own entry and a late response cannot overwrite a newer
period's view. The keys need no change; what was broken was only what the fetcher put on
the wire under them. The existing cache entries hold monthly bodies under daily/weekly
keys, and they die with the page — no invalidation step is needed at deploy.

**Loading and error states stay as they are.** During a switch the page shows its skeleton
(`BudgetSpend.tsx:150`) and `MySpend` — mounted unconditionally by the #4686 ruling so the
limit editor survives an envelope outage — shows "Your spend could not be read… This is not
a statement that it is zero" (`SpendTiles.tsx:199-201`). Neither displays the previous
period's figures, which is what the acceptance criterion asks. No `placeholderData` or
`keepPreviousData` is set anywhere in this surface, and none should be added: showing the
old period's numbers while the new one loads is precisely the confusion being fixed.

---

## 4. Changes

### 4.1 Source (the fix)

`modules/gateway/frontend/src/services/budgetSpend.ts` — the only production file.

- `getMyBudget`: `buildQueryString({ period_type: period })`.
- `getMyBudgetRuns`: `buildQueryString({ period_type: period, page_size: pageSize, cursor: cursor ?? undefined })`.
- Optional per D2: the response-period assertion in both, plus a one-line note in the
  module header recording that the wire key is `period_type` and why.

`buildQueryString` (`services/api.ts:107`) needs no change — it appends keys verbatim and
drops `undefined`/`null`/`''`, which is what keeps `cursor: null` off a first-page request.

### 4.2 Fixtures and mocks (the reason this was invisible)

`mocks/handlers/budget.ts:190-192` returns fixed bodies for both routes regardless of
query. Make the two handlers **behave like the routes**: read `period_type`, default to
`monthly` when absent, ignore unknown parameters, and answer with that period's fixture.
That single change is what makes a wrong key observable in every test that goes through MSW.

`mocks/data/budgetSpend.ts` gains per-period fixtures with distinct windows and distinct
money, following the existing `mockPersonDefaultFor(scope, period)` precedent in the same
file (`:360`) — e.g. `mockBudgetEnvelopeFor(period)` and `mockBudgetRunsFor(period)`
derived from the current monthly fixtures. Distinct *amounts* matter as much as distinct
dates: an assertion on dates alone would miss a client that fetched the right window and
rendered the wrong body. Provenance rule from that file's header still applies — shapes
are transcribed from `src/budget/schemas.py`.

### 4.3 Tests

`__tests__/services/budgetSpend.test.ts` currently asserts the defect:
`expect(seen?.searchParams.get('period')).toBe('weekly')` at :38 and the same for `daily`
at :66. Correct both to `period_type`, and add `expect(seen?.searchParams.has('period')).toBe(false)`
so the obsolete key cannot come back beside the new one. Keep the 503-propagates,
page-size/cursor, cursor-omitted and no-`user_id`/`entity_id` tests as they are.

`__tests__/components/BudgetSpend.test.tsx` mocks both fetchers (`:42-48`) and asserts
`getMyBudget` was *called with* `'daily'` (`:258`). That assertion is true on the broken
code — it is the gap. Leave the file's genuine page concerns alone and add one new
render-level spec that does **not** mock `services/budgetSpend`, letting MSW answer:
select each tab in turn and assert the rendered date window, headline spend and runs rows
are that period's fixture. New file, following the existing `__tests__/pages/` convention,
e.g. `__tests__/pages/BudgetSpendPeriodWiring.test.tsx`.

**The gate: that spec must fail on `662906e` and pass with §4.1.** A test that passes both
ways has not covered this defect. If D2 is accepted, add one spec that a mismatched
`period.period_type` in the response surfaces the error affordance rather than a figure.

### 4.4 Explicitly not in scope

- **No run pagination.** No `onLoadMore`, no cursor state, no `useInfiniteQuery`. None
  exists today (§0.2); adding it is a feature and would need its own reset-on-period-change
  design. The service keeps accepting `pageSize`/`cursor` for its existing callers and tests.
- **No backend change** — no route, schema, migration, Lambda, Terraform or IAM change (§2).
- **No pricing, refresh or seeding work** — that is #4969, and correcting rates cannot fix
  these tabs. No historical spend is rewritten; this is a read-path defect only.
- **No managed-scope client.** `managed_scope_routes.py` declares the same parameter
  correctly and has no frontend caller to fix.
- **No change to the one-headline-figure ruling** (#4669/#4685) or to any error/unknown
  affordance.

---

## 5. Rollout, verification, rollback

**On merge**, `gateway-deploy.yml` fires on `modules/gateway/frontend/**`: its change
filter selects the frontend job only, which builds with `VITE_API_URL=/api`, syncs `dist/`
to S3 excluding `cfn-templates/*`, and invalidates CloudFront `/*`. The backend image,
migrations, Lambdas and Terraform are **not** touched. `gateway-ci.yml`'s
`Frontend Unit Tests` job (`npx vitest run`) runs the new specs on the PR.

**Pre-submit**, from `modules/gateway/frontend`: `npm ci`, `npx vitest run`, `npm run lint`.
No Python or Terraform checks apply — the diff does not reach those modules.

**Live check after deploy**, signed in on the dashboard: for each of Daily, Weekly and
Monthly, confirm the network request is `/api/me/budget?period_type=<tab>`, the response
`period.period_type` equals the tab and the rendered date window matches it; then expand
agent runs and confirm `/api/me/budget/runs?period_type=<tab>` likewise. Read-only, no
writes. Attach the evidence to #4970 before closing, per its own criteria.

**Rollback** is `git revert` of the PR: the change is code-only, so re-running the frontend
job on the reverted commit restores the previous bundle and invalidates the cache. No data
migration and no state to unwind.

---

## 6. Decisions for the issue owner

| ID | Question | Recommendation |
|---|---|---|
| **D1** | Accept `period` server-side as an alias for compatibility? | **No** — client-only fix (§2). The alternative permanently widens the API for one client's bug; the stale-bundle window shows today's behaviour, not a new one. |
| **D2** | Add the client-side check that the response's period matches the requested one? | **Yes** — four lines that convert this whole defect class into the page's existing error state (§2.1). Reject it if you prefer the absolutely minimal diff; the §4.1 fix stands alone without it. |
| **D3** | Confirm run pagination stays out of scope? | **Yes, out of scope** — no cursor state exists to scope (§4.4). The issue's cursor criterion is satisfied vacuously today; building pagination here needs its own design. |

**Assumptions.** (a) The issue's live API comparison of 2026-09-12 04:14 UTC is taken as
given; this review verified the *source* contract on `662906e` and did not re-run live
requests. (b) The deployed bundle names in the issue (`BudgetSpend-CAuWD0dD.js`,
`personCap-D7_ngWvP.js`) are taken as given and were not re-fetched. (c) No user-visible
behaviour outside the three tabs and the runs drill-down is expected to change; the
regression bar is the existing `BudgetSpend.test.tsx`, `SpendTiles.test.tsx` and
`BudgetLines.test.tsx` suites continuing to pass unmodified except for the two corrected
assertions in the service test.

**Not verified here.** Whether the daily/weekly ledger rows are themselves complete for a
given caller. This design fixes which period is requested and rendered; it makes no claim
about the accuracy of the figures inside a period, which is #4969's territory.
