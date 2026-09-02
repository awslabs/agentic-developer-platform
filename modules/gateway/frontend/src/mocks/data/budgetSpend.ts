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

import type { BudgetEnvelopeResponse, BudgetLine, BudgetRunsResponse, PerOrgLine, PersonCapResponse, PersonEnvelope } from '@/types/budget';

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
 * The caller's per-tenant cloud lines — `PerOrgLine`, Issue #4626. Active partition FIRST.
 *
 * This is the #4620 operator scenario, reproduced: the person is a member of two
 * tenants, their session is attributed to the first, and their agent runs also execute
 * in the second. The active line's `cloud_spend_usd` deliberately equals
 * `mockCloudLine.spend_usd` — the schema orders the active partition first and flags it
 * precisely so these rows AGREE with the `lines`/`binding` figures rendered beside them,
 * and a fixture where they disagreed would be describing an impossible response.
 *
 * The second line is the defect's signature: real settled spend in a tenant that
 * authored **no** cap (`cap_usd: null`). Rendering that as `$0.00` would report a $0
 * ceiling where the truth is "no ceiling was ever authored here", which is the confusion
 * the whole issue is about.
 */
export const mockPerOrgLines: PerOrgLine[] = [
  {
    org_id: 'org-1',
    org_name: 'Pranav Sharma (home)',
    // Matches mockCloudLine.spend_usd — the active partition is the one `lines` describes.
    cloud_spend_usd: '171.400000',
    // …and mockCloudLine.cap_usd: this tenant authored the cap.
    cap_usd: '200.00',
    is_active_partition: true,
  },
  {
    org_id: 'org-aws-e',
    org_name: 'aws-e',
    // Real dollars, in the partition the caller's session is NOT attributed to. Invisible
    // on this screen before #4646 — the `$0` the issue was filed for.
    cloud_spend_usd: '243.650000',
    // No cap authored in the tenant where the spend actually accrues.
    cap_usd: null,
    is_active_partition: false,
  },
];

/**
 * `PersonEnvelope` — the cross-org sum. **Informational; never a budget.**
 *
 * Note which fields are ABSENT and that their absence is the contract: no `cap_usd`, no
 * `remaining_usd`, no `utilization_pct`, no `band`. None exists on the wire, which is
 * what makes a progress bar unbindable rather than merely discouraged. `is_budget` is an
 * unsettable `false` for the same reason.
 *
 * `spend_usd` is the exact 6dp sum of `mockPerOrgLines` (171.400000 + 243.650000).
 * `anchor` is `github:<numeric id>` because one person can hold a different `users.id`
 * per tenant, so the GitHub identity — not the canonical id — is what the totals fuse on.
 */
export const mockPersonEnvelope: PersonEnvelope = {
  anchor: 'github:12345678',
  spend_usd: '415.050000',
  partition_count: 2,
  is_budget: false,
  note: "Your cloud-agent spend across every workspace you belong to. Not a cap: no budget governs this total, and nothing is enforced against it. Each workspace's own cap is shown on its line.",
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
  updated_at: '2026-08-30T12:00:00Z',
};

/** A person with an ENFORCING limit — what #4630 writes on every save. */
export const mockPersonCapEnforcing: PersonCapResponse = {
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: '250.00',
  cap_status: 'capped',
  enforcement_mode: 'hard',
  updated_at: '2026-09-01T12:00:00Z',
};

/** A person with NO limit set. `cap_usd` is `null`, never `'0.00'`. */
export const mockPersonCapUncapped: PersonCapResponse = {
  person_anchor: 'github:5550001',
  period_type: 'monthly',
  cap_usd: null,
  cap_status: 'uncapped',
  enforcement_mode: null,
  updated_at: null,
};
