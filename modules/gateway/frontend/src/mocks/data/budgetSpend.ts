/**
 * Fixtures for the caller's budget read surface — Issue #4402 (U-5).
 *
 * **PROVENANCE (mandatory, and the point of this file): every field below is
 * transcribed from `modules/gateway/src/budget/schemas.py`** — `MyBudgetResponse`,
 * `BudgetLine`, `CombinedInformational`, `Freshness`, `MyBudgetRunsResponse`,
 * `BudgetRunItem`, `CostFigure`. Not from `src/types/budget.ts`, and not from what a
 * component happens to read.
 *
 * That rule is not ceremony. #3675 shipped a dashboard whose TypeScript types, MSW
 * mocks, unit tests *and* end-to-end evaluation all agreed on fields the backend never
 * sent — every artefact was derived from the same invented shape, so the loop was
 * closed and nothing in CI could see the gap. Production rendered `undefined`. Deriving
 * fixtures from the backend model is the one link that keeps the loop open: if the wire
 * shape changes, these fixtures are wrong in the same direction as the code.
 *
 * Precision matters and is reproduced deliberately, per contract rule 1:
 *   - caps at **2dp** (`NUMERIC(10,2)`)      → `'600.00'`
 *   - spend/headroom at **6dp** (`NUMERIC(14,6)`) → `'412.800000'`
 *   - all money is a **string**, never a number
 *
 * Also reproduced: `remaining_usd` can be negative; an `unknown` `CostFigure` carries
 * **no** `amount_usd` and **always** a `reason` (a backend validator enforces both);
 * `utilization_pct` is `null` for an uncapped line.
 */

import type {
  BudgetBand,
  BudgetEnvelopeResponse,
  BudgetLine,
  BudgetPeriodType,
  BudgetRunsResponse,
  PerOrgLine,
  PersonCapResponse,
  PersonDefaultResponse,
  PersonEnvelope,
} from '@/types/budget';

/** `BudgetPeriod` — only calendar periods exist; run/chain have no window. */
export const mockPeriod = {
  period_type: 'monthly' as const,
  period_start: '2026-08-01',
  period_end: '2026-08-31',
  resets_in_days: 1,
};

/** The caller's `direct` line: their own traffic, keyed by Cognito sub. */
export const mockDirectLine: BudgetLine = {
  entity_type: 'user',
  label: 'Direct use (my machine)',
  source: 'direct',
  principal_kind: 'human',
  cap_usd: '600.00',
  spend_usd: '412.800000',
  remaining_usd: '187.200000',
  utilization_pct: 68.8,
  band: 'none',
  cap_status: 'capped',
  enforcement_mode: 'shadow',
};

/** The caller's `cloud` line: chains they triggered, keyed by canonical `users.id`. */
export const mockCloudLine: BudgetLine = {
  entity_type: 'root_user',
  label: 'Cloud agent runs',
  source: 'cloud',
  principal_kind: 'human',
  cap_usd: '200.00',
  spend_usd: '171.400000',
  remaining_usd: '28.600000',
  utilization_pct: 85.7,
  band: 'warning',
  cap_status: 'capped',
  enforcement_mode: 'shadow',
};

/**
 * A `service:`-rooted line — an unattended trigger (CI, EventBridge, an alarm).
 * `principal_kind: 'service'` is what keeps it from being rendered as a colleague.
 */
export const mockServiceLine: BudgetLine = {
  entity_type: 'root_user',
  label: 'CI automation',
  source: 'cloud',
  principal_kind: 'service',
  cap_usd: '50.00',
  spend_usd: '12.500000',
  remaining_usd: '37.500000',
  utilization_pct: 25.0,
  band: 'none',
  cap_status: 'capped',
  enforcement_mode: 'shadow',
};

/**
 * An uncapped line: no budget row governs it, so cap/headroom/utilisation/band are ALL
 * `null`. This is NOT a `$0` cap — that would be `cap_status: 'capped'` with
 * `cap_usd: '0.00'` and `band: 'exceeded'`.
 */
export const mockUncappedLine: BudgetLine = {
  entity_type: 'user',
  label: 'Direct use (my machine)',
  source: 'direct',
  principal_kind: 'human',
  cap_usd: null,
  spend_usd: '31.250000',
  remaining_usd: null,
  utilization_pct: null,
  band: null,
  cap_status: 'uncapped',
  enforcement_mode: null,
};

/**
 * The caller's per-tenant lines — `PerOrgLine`, Issue #4626, both spend components
 * since #4396. Active partition FIRST.
 *
 * This is the #4620 operator scenario, reproduced: the person is a member of two
 * tenants, their session is attributed to the first, and their agent runs also execute
 * in the second. The active line's `cloud_spend_usd` deliberately equals
 * `mockCloudLine.spend_usd`, and its `direct_spend_usd` equals `mockDirectLine.spend_usd`
 * — the schema orders the active partition first and flags it precisely so these rows
 * AGREE with the `lines`/`binding` figures rendered beside them, and a fixture where they
 * disagreed would be describing an impossible response.
 *
 * The second line is the defect's signature: real settled spend in a tenant that
 * authored **no** cap (`cap_usd: null`). Rendering that as `$0.00` would report a $0
 * ceiling where the truth is "no ceiling was ever authored here", which is the confusion
 * the whole issue is about. It carries a `direct_spend_usd` of `'0.000000'` — a person's
 * interactive spend lands in whichever tenant they were signed into, so a partition with
 * agent runs and no direct use is the ordinary shape, and a true `'0.000000'` is a
 * measurement rather than an absence.
 */
export const mockPerOrgLines: PerOrgLine[] = [
  {
    org_id: 'org-1',
    org_name: 'Pranav Sharma (home)',
    // Matches mockCloudLine.spend_usd — the active partition is the one `lines` describes.
    cloud_spend_usd: '171.400000',
    // …and mockDirectLine.spend_usd, for the same reason.
    direct_spend_usd: '412.800000',
    // …and mockCloudLine.cap_usd: this tenant authored the cap. Governs the CLOUD
    // figure only — one org's `root_user` row — never the line's total.
    cap_usd: '200.00',
    is_active_partition: true,
  },
  {
    org_id: 'org-aws-e',
    org_name: 'aws-e',
    // Real dollars, in the partition the caller's session is NOT attributed to. Invisible
    // on this screen before #4646 — the `$0` the issue was filed for.
    cloud_spend_usd: '243.650000',
    direct_spend_usd: '0.000000',
    // No cap authored in the tenant where the spend actually accrues.
    cap_usd: null,
    is_active_partition: false,
  },
];

/**
 * `PersonEnvelope` — the person's cross-org TOTAL, and since #4396 the figure their
 * personal limit is enforced against.
 *
 * Note which fields are still ABSENT and that their absence is the contract: no
 * `cap_usd`, no `remaining_usd`, no `utilization_pct`, no `band`. The ceiling is real
 * now, but it has one home (`GET /me/budget/person-cap`) — so a progress bar remains
 * unbindable from this object rather than merely discouraged, and the two surfaces cannot
 * disagree about one limit. `is_budget` is an unsettable `false` for the same reason: it
 * says THIS OBJECT carries no denominator, not that the figure is ungoverned.
 *
 * `spend_usd` is the exact 6dp sum of BOTH components of `mockPerOrgLines`
 * (171.400000 + 243.650000 cloud, 412.800000 direct), and `cloud_spend_usd` /
 * `direct_spend_usd` are those two subtotals. `anchor` is `github:<numeric id>` because
 * one person can hold a different `users.id` per tenant, so the GitHub identity — not the
 * canonical id — is what the totals fuse on. `note` is transcribed verbatim from
 * `_person_envelope` in `src/budget/me_routes.py`.
 */
export const mockPersonEnvelope: PersonEnvelope = {
  anchor: 'github:12345678',
  spend_usd: '827.850000',
  cloud_spend_usd: '415.050000',
  direct_spend_usd: '412.800000',
  partition_count: 2,
  is_budget: false,
  note:
    "Everything you have spent — your own direct use plus the agent runs you triggered — across every GitHub org you belong to. This is the figure a personal spending limit for the monthly period is enforced against; a limit on another period is checked against that period's own total.",
};

/**
 * The full envelope. The headline fields mirror `binding` — the lowest-remaining
 * CAPPED line — and are never the sum of `lines`.
 */
export const mockBudgetEnvelope: BudgetEnvelopeResponse = {
  period: mockPeriod,
  entity_type: 'root_user',
  cap_usd: '200.00',
  spend_usd: '171.400000',
  remaining_usd: '28.600000',
  utilization_pct: 85.7,
  band: 'warning',
  cap_status: 'capped',
  enforcement_mode: 'shadow',
  identity_status: 'resolved',
  binding: mockCloudLine,
  lines: [mockDirectLine, mockCloudLine],
  combined_informational: {
    // Note there is no cap/remaining/utilisation/band field here — none exists on the
    // wire, which is what makes a progress bar unbindable rather than merely discouraged.
    spend_usd: '584.200000',
    is_budget: false,
    note: 'Sum of two separately-capped lines. No cap governs this total and it is not enforced.',
  },
  // The cross-org view (#4626). Both fields are OPTIONAL on the response type because a
  // backend predating #4640 omits them — see the "renders unchanged" test.
  per_org: mockPerOrgLines,
  person_envelope: mockPersonEnvelope,
  freshness: {
    cost_backfill_lag: false,
  },
};

/** The run drill-down. Page-scoped `subtotal` and `total_run_count`. */
export const mockBudgetRuns: BudgetRunsResponse = {
  items: [
    {
      run_id: 'evt-0001',
      correlation_id: 'corr-aaa',
      persona: 'developer',
      started_at: '2026-08-30T08:31:15Z',
      status: 'complete',
      cost: { status: 'known', amount_usd: '1.284500', reason: null, scope: 'agent run costs only; excludes build/infra', partial: false },
      attribution: 'cloud',
    },
    {
      run_id: 'evt-0002',
      correlation_id: 'corr-bbb',
      persona: 'reviewer',
      started_at: '2026-08-30T07:02:00Z',
      status: 'in_progress',
      // `unknown` carries NO amount and ALWAYS a reason — a backend validator refuses
      // any other shape, because an `unknown` holding `0` is how absence becomes $0.00.
      cost: { status: 'unknown', amount_usd: null, reason: 'no_usage_rows', scope: 'agent run costs only; excludes build/infra', partial: false },
      attribution: 'cloud',
    },
    {
      run_id: 'evt-0003',
      correlation_id: null,
      persona: 'architect',
      started_at: '2026-08-29T18:45:00Z',
      // A verified zero: rows exist and total zero. `$0.00` is honest here.
      cost: { status: 'none_incurred', amount_usd: '0.000000', reason: null, scope: 'agent run costs only; excludes build/infra', partial: false },
      status: 'complete',
      attribution: 'direct',
    },
  ],
  // `partial: true` because one run on the page is `unknown` — this total is a LOWER BOUND.
  subtotal: { status: 'known', amount_usd: '1.284500', reason: null, scope: 'agent run costs only; excludes build/infra', partial: true },
  total_run_count: 3,
  next_cursor: null,
  period: mockPeriod,
  identity_status: 'resolved',
};

// ---------------------------------------------------------------------------
// The person-level cap — Issue #4629 (#4620 · C3)
// ---------------------------------------------------------------------------
//
// PROVENANCE, same rule as above: transcribed from `PersonCapResponse` in
// `modules/gateway/src/budget/schemas.py`, not from `types/budget.ts` and not
// from what the component happens to read.
//
// Note what these fixtures deliberately reproduce:
//   - `cap_usd` at **2dp** as a **string** (`NUMERIC(10,2)`, contract rule 1)
//   - the uncapped shape carries `cap_usd: null` and `enforcement_mode: null` —
//     NOT `'0.00'`, because "no limit" and "a limit of zero" are different states
//   - both `enforcement_mode` values, because the API returns both: #4630 made the
//     layer enforce and writes `hard`, but rows authored under C3 keep the `soft`
//     they were saved with until the person re-saves, so the UI has to render each

/** A person with a C3-era informational limit — still `soft` until re-saved (#4630). */
export const mockPersonCap: PersonCapResponse = {
  // `github:<numeric_id>`, not a `users.id` — see the type's own comment.
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: '250.00',
  cap_status: 'capped',
  enforcement_mode: 'soft',
  // A soft row can only be pre-ruling self-authored (#4690) — hence `own`.
  source: 'own',
  source_label: 'your own limit',
  updated_at: '2026-08-30T12:00:00Z',
};

/** A person with an ENFORCING limit — what #4630 writes on every save. */
export const mockPersonCapEnforcing: PersonCapResponse = {
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: '250.00',
  cap_status: 'capped',
  enforcement_mode: 'hard',
  source: 'admin',
  source_label: 'a limit set for you by a platform administrator',
  updated_at: '2026-09-01T12:00:00Z',
};

/**
 * A person governed by a DEFAULT rung (#4690) — they authored nothing, no admin
 * wrote an individual row, and an org-wide rule caps them anyway. `updated_at` is
 * `null` on purpose: default timestamps are withheld from person-facing surfaces.
 */
export const mockPersonCapDefaultGoverned: PersonCapResponse = {
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: '100.00',
  cap_status: 'capped',
  // Defaults are ALWAYS hard — `ck_person_budget_default_hard` pins it in the DB.
  enforcement_mode: 'hard',
  source: 'org_default',
  source_label: 'org default for org-acme',
  updated_at: null,
};

/** A person with NO limit set. `cap_usd` is `null`, never `'0.00'`. */
export const mockPersonCapUncapped: PersonCapResponse = {
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: null,
  cap_status: 'uncapped',
  enforcement_mode: null,
  source: null,
  source_label: null,
  updated_at: null,
};

// ---------------------------------------------------------------------------
// DEFAULT person limits — Issue #4690 (D1), rendered by #4691 (D2).
//
// PROVENANCE: transcribed from `PersonDefaultResponse` in `src/budget/schemas.py`.
// Two contract rules carry over unchanged from the cap shapes above:
//   - money is a STRING at 2dp, the `NUMERIC(10,2)` column's precision
//   - "no rule authored" is `cap_usd: null` + `cap_status: 'uncapped'`, NOT `'0.00'`
//     — a zeroed default would read as "nobody in this scope may spend anything"
//
// Unlike `PersonCapResponse` there is no `soft` variant to model: the route writes
// `hard` unconditionally and rejects anything else, so a soft default is a shape the
// server cannot produce and a fixture for it would model an impossible response.
// ---------------------------------------------------------------------------

/**
 * Build the default-rule response for one scope path segment and period.
 *
 * The scope is **parsed back out of the path** (`platform` | `org:<id>` |
 * `team:<org>:<team>`) rather than canned, so a handler using this echoes the ids the
 * component actually sent. A fixture with hardcoded ids would pass whether or not the
 * component built the segment correctly — the #4511 guard, applied to the scope.
 *
 * Only `platform` answers `capped`. That models a fresh install where the broadest
 * rule has been authored and nothing narrower has, which keeps BOTH the "rule set"
 * and "No rule set" states reachable in mock mode without a stateful handler.
 */
export function mockPersonDefaultFor(scopeSegment: string, period: string): PersonDefaultResponse {
  const parts = decodeURIComponent(scopeSegment).split(':');
  const scopeType = parts[0] === 'org' || parts[0] === 'team' ? parts[0] : 'platform';
  const isPlatform = scopeType === 'platform';

  return {
    scope_type: scopeType,
    scope_id_org: isPlatform ? null : (parts[1] ?? null),
    scope_id_team: scopeType === 'team' ? (parts[2] ?? null) : null,
    period_type: period as BudgetPeriodType,
    cap_usd: isPlatform ? '1000.00' : null,
    cap_status: isPlatform ? 'capped' : 'uncapped',
    // Always `hard` when a rule exists — `ck_person_budget_default_hard` pins it.
    enforcement_mode: isPlatform ? 'hard' : null,
    updated_at: isPlatform ? '2026-09-05T09:00:00Z' : null,
  };
}

/** The platform rung with a rule authored — the seeded state of the mock above. */
export const mockPersonDefaultPlatform: PersonDefaultResponse = mockPersonDefaultFor('platform', 'monthly');

/** A scope with NO rule of its own. `cap_usd` is `null`, never `'0.00'`. */
export const mockPersonDefaultUncapped: PersonDefaultResponse = mockPersonDefaultFor('org:org-acme', 'monthly');

// ---------------------------------------------------------------------------
// PER-PERIOD fixtures — Issue #4970 (implementation child #4973)
// ---------------------------------------------------------------------------
//
// Why these exist: every fixture above describes ONE period (monthly), and the MSW
// handlers answered with them whatever the query. So a client that asked for `daily`
// under the wrong wire key got a monthly body and every test still passed — which is
// precisely how #4970 shipped and stayed invisible. Period-aware fixtures plus
// period-aware handlers are what make a wrong key observable in CI.
//
// Both DATES and MONEY differ per period, deliberately. Distinct windows alone would
// catch a client that requested the wrong period, but not one that requested the right
// window and rendered another period's body — so the amounts are distinct too, and a
// rendering assertion on either one fails on the pre-fix mapping.
//
// PROVENANCE is unchanged and still the point (see this file's header): these are the
// same `MyBudgetResponse` / `MyBudgetRunsResponse` shapes transcribed from
// `src/budget/schemas.py`, with only the period window and figures varied. Money stays
// a STRING — caps at 2dp, spend/headroom at 6dp — and `utilization_pct`/`band` are
// derived exactly as `_band_for` (`me_routes.py:304`) derives them, off the 80/95
// thresholds, so no fixture describes a response the backend could not produce.
//
// Follows the `mockPersonDefaultFor(scope, period)` precedent above: one function
// per response shape, keyed by period, rather than nine hand-maintained constants.

/** The three calendar windows, as the routes' `_resolve_period_bounds` would resolve them. */
const PERIOD_WINDOWS: Record<BudgetPeriodType, { period_start: string; period_end: string; resets_in_days: number }> = {
  // A single day: start and end are the same date and the counter resets tomorrow.
  daily: { period_start: '2026-08-30', period_end: '2026-08-30', resets_in_days: 0 },
  // Monday-anchored ISO week containing that day.
  weekly: { period_start: '2026-08-24', period_end: '2026-08-30', resets_in_days: 0 },
  // The calendar month — the same window `mockPeriod` above describes.
  monthly: { period_start: '2026-08-01', period_end: '2026-08-31', resets_in_days: 1 },
};

/**
 * Spend per period for the caller's cloud line, against a constant `200.00` cap.
 *
 * Strictly increasing with the window's length, because a longer window contains the
 * shorter one's runs: a daily figure LARGER than the monthly one would be an
 * impossible response and a test asserting on it would pin nonsense. The chosen
 * figures also land in three different bands, so a tab swap changes the badge as well
 * as the number.
 */
const CLOUD_SPEND_BY_PERIOD: Record<BudgetPeriodType, { spend: string; remaining: string; pct: number; band: BudgetBand }> = {
  // 14.20 / 200 = 7.1% — `none`.
  daily: { spend: '14.200000', remaining: '185.800000', pct: 7.1, band: 'none' },
  // 96.55 / 200 = 48.3% (48.275 rounded to 1dp) — still `none`.
  weekly: { spend: '96.550000', remaining: '103.450000', pct: 48.3, band: 'none' },
  // 171.40 / 200 = 85.7% — `warning`, the monthly figures the fixtures above carry.
  monthly: { spend: '171.400000', remaining: '28.600000', pct: 85.7, band: 'warning' },
};

/** Direct-use spend per period, against a constant `600.00` cap. Same monotonicity rule. */
const DIRECT_SPEND_BY_PERIOD: Record<BudgetPeriodType, { spend: string; remaining: string; pct: number }> = {
  // 22.90 / 600 = 3.8% (3.816… → 3.8).
  daily: { spend: '22.900000', remaining: '577.100000', pct: 3.8 },
  // 148.35 / 600 = 24.7% (24.725 → 24.7).
  weekly: { spend: '148.350000', remaining: '451.650000', pct: 24.7 },
  // 412.80 / 600 = 68.8%.
  monthly: { spend: '412.800000', remaining: '187.200000', pct: 68.8 },
};

/** Cloud spend in the caller's SECOND (non-active) partition, per period. */
const OTHER_ORG_CLOUD_BY_PERIOD: Record<BudgetPeriodType, string> = {
  daily: '31.500000',
  weekly: '132.900000',
  monthly: '243.650000',
};

/** `BudgetPeriod` for one period — the object both routes echo as `period`. */
export function mockPeriodFor(period: BudgetPeriodType) {
  return { period_type: period, ...PERIOD_WINDOWS[period] };
}

/**
 * The full `/me/budget` envelope for one period.
 *
 * Every internal agreement the monthly fixtures maintain is maintained here at each
 * period, because a fixture whose parts disagree describes a response the routes
 * cannot compose: the headline mirrors the BINDING (cloud) line rather than the sum of
 * `lines`; the active partition's `cloud_spend_usd`/`direct_spend_usd` equal the cloud
 * and direct lines beside them; `person_envelope.spend_usd` is the exact 6dp sum of
 * both components of both partitions; and the `note` interpolates this period, as
 * `_person_envelope` does.
 */
export function mockBudgetEnvelopeFor(period: BudgetPeriodType): BudgetEnvelopeResponse {
  const cloud = CLOUD_SPEND_BY_PERIOD[period];
  const direct = DIRECT_SPEND_BY_PERIOD[period];
  const otherOrgCloud = OTHER_ORG_CLOUD_BY_PERIOD[period];

  const cloudLine: BudgetLine = { ...mockCloudLine, spend_usd: cloud.spend, remaining_usd: cloud.remaining, utilization_pct: cloud.pct, band: cloud.band };
  const directLine: BudgetLine = {
    ...mockDirectLine,
    spend_usd: direct.spend,
    remaining_usd: direct.remaining,
    utilization_pct: direct.pct,
    // Direct use is under 80% at every period, so it is never banded above `none`.
    band: 'none',
  };

  const perOrg: PerOrgLine[] = [
    { ...mockPerOrgLines[0], cloud_spend_usd: cloud.spend, direct_spend_usd: direct.spend },
    { ...mockPerOrgLines[1], cloud_spend_usd: otherOrgCloud, direct_spend_usd: '0.000000' },
  ];

  const personTotal = (Number(cloud.spend) + Number(direct.spend) + Number(otherOrgCloud)).toFixed(6);
  const cloudTotal = (Number(cloud.spend) + Number(otherOrgCloud)).toFixed(6);

  return {
    ...mockBudgetEnvelope,
    period: mockPeriodFor(period),
    // The headline mirrors the binding (cloud) line — never the sum of `lines`.
    spend_usd: cloud.spend,
    remaining_usd: cloud.remaining,
    utilization_pct: cloud.pct,
    band: cloud.band,
    binding: cloudLine,
    lines: [directLine, cloudLine],
    combined_informational: {
      // The sum of the two separately-capped lines, which no cap governs.
      spend_usd: (Number(cloud.spend) + Number(direct.spend)).toFixed(6),
      // Built explicitly rather than spread: the field is OPTIONAL on the response type
      // (an older backend omits it), so spreading it widens `is_budget` to
      // `false | undefined` — and the type pins it to the literal `false` on purpose,
      // because "this object carries no denominator" is not a settable flag.
      is_budget: false,
      note: mockBudgetEnvelope.combined_informational!.note,
    },
    per_org: perOrg,
    person_envelope: {
      ...mockPersonEnvelope,
      spend_usd: personTotal,
      cloud_spend_usd: cloudTotal,
      direct_spend_usd: direct.spend,
      // `_person_envelope` interpolates the period into the note; a fixture repeating
      // "monthly" on the daily tab would model an impossible response.
      note: mockPersonEnvelope.note.replace('monthly period', `${period} period`),
    },
  };
}

/**
 * The `/me/budget/runs` page for one period.
 *
 * Run COUNT varies with the window as well as the money: the daily page carries the
 * one run that started inside its day, the weekly page adds an earlier run from the
 * same week, and the monthly page is the three-run fixture above. So an assertion on
 * the number of rendered rows distinguishes the three periods on its own, and each
 * page's `subtotal`/`total_run_count` still describe THAT PAGE, per the type's rule.
 */
export function mockBudgetRunsFor(period: BudgetPeriodType): BudgetRunsResponse {
  const all = mockBudgetRuns.items;
  // Newest first, as the route returns them: slice from the front so each period is a
  // prefix of the longer window's page.
  const itemsByPeriod: Record<BudgetPeriodType, typeof all> = {
    daily: all.slice(0, 1),
    weekly: all.slice(0, 2),
    monthly: all,
  };
  const items = itemsByPeriod[period];

  // Sum the KNOWN costs only, and mark the subtotal partial when any run on the page is
  // `unknown` — an `unknown` contributes no amount, because a $0 stand-in is how
  // absence becomes a figure.
  const known = items.filter((item) => item.cost.status === 'known' || item.cost.status === 'none_incurred');
  const partial = items.some((item) => item.cost.status === 'unknown');
  const amount = known.reduce((total, item) => total + Number(item.cost.amount_usd ?? 0), 0).toFixed(6);

  return {
    ...mockBudgetRuns,
    items,
    subtotal: { ...mockBudgetRuns.subtotal, amount_usd: amount, partial },
    total_run_count: items.length,
    period: mockPeriodFor(period),
  };
}

/**
 * The caller's personal limit for one period — the headline figure's DENOMINATOR.
 *
 * Period-aware for the same reason the envelope is: the limit is a per-period row
 * (`person_budget_configs` is keyed by period), the client already sent the right wire
 * key before #4970, and a canned monthly cap would make the denominator agree with
 * every tab — hiding whether the numerator and denominator describe the SAME period,
 * which is the mismatch the defect made visible on screen.
 *
 * A shorter window gets a smaller limit, the ordering a person would actually author.
 * `enforcement_mode` is `hard` at every period (what #4630 writes on save), so the bar
 * is drawable and its percentage is checkable per tab.
 */
export function mockPersonCapFor(period: BudgetPeriodType): PersonCapResponse {
  const capByPeriod: Record<BudgetPeriodType, string> = {
    daily: '25.00',
    weekly: '120.00',
    monthly: '250.00',
  };

  return { ...mockPersonCapEnforcing, period_type: period, cap_usd: capByPeriod[period] };
}
