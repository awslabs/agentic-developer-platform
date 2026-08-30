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

import type { BudgetEnvelopeResponse, BudgetLine, BudgetRunsResponse } from '@/types/budget';

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
  label: 'Direct usage (my machine)',
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
  label: 'Direct usage (my machine)',
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
