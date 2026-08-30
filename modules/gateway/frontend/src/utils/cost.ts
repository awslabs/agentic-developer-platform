/**
 * The single cost formatter (issue #4207).
 *
 * Before this module there were five: `formatCurrency` in `utils/format.ts`, a
 * byte-identical local copy in `BudgetManagement.tsx`, `formatCost` in
 * `InvocationDetail.tsx`, another in `ActivityCard.tsx`, and the pair inside
 * `AgentActivity.tsx`'s cost badges — plus five more copy-pasted inline
 * `toFixed(4|2)` expressions. They disagreed on two things that matter:
 *
 *   1. **Precision.** `Intl.NumberFormat` fixed-2 renders `$0.005` as `"$0.01"`;
 *      the `toFixed(4)`-under-a-cent convention renders it `"$0.0050"`. Most
 *      individual agent calls are sub-cent, so fixed-2 rounds the common case to
 *      either nothing or a whole cent. The sub-cent convention wins here.
 *   2. **Absence.** `formatCurrency` coerced `null`/`undefined`/`NaN` to `0` and
 *      returned `"$0.00"`, so "we don't know what this cost" rendered
 *      identically to "this was free". That is the bug this story exists to end.
 *
 * **Three-valued cost.** The API returns a status per figure, never a bare
 * number (`src/orchestration/cost.py`):
 *
 *   - `known`          → render the amount
 *   - `none_incurred`  → a *verified* zero. `$0.00` is correct and honest here.
 *   - `unknown`        → no ledger row. Renders `NO_DATA_INDICATOR`, **never**
 *                        a currency string.
 *
 * `none_incurred` and `unknown` both total zero dollars and mean opposite
 * things, which is precisely why the status is carried on the wire instead of
 * being inferred from the amount.
 *
 * The no-data convention is `SpendTodayTile`'s existing one (`'—'` plus an
 * explanatory tooltip, issue #3633 / REQ-A5), lifted rather than reinvented —
 * a third convention for the same idea was how five formatters happened.
 */

/** What a figure renders as when it is not known. Never a currency string. */
export const NO_DATA_INDICATOR = '—';

/**
 * Scope disclaimer for every cost figure (R-N5c).
 *
 * These totals cover agent-run Bedrock spend only — not CodeBuild, EKS compute,
 * NAT, or storage. A figure that silently excludes non-run cost reads as the
 * total cost, and someone will make a budget decision on it.
 */
export const COST_SCOPE_LABEL = 'agent run costs only; excludes build/infra';

/** The three-valued cost status, mirroring `CostStatus` in `cost.py`. */
export type CostStatus = 'known' | 'none_incurred' | 'unknown';

/** A cost figure as it arrives from the API. */
export interface CostFigure {
  status: CostStatus;
  /** Serialised `Numeric(10, 6)`. A string so sub-cent precision survives JSON. */
  amount_usd?: string | null;
  reason?: string | null;
  scope?: string | null;
  /**
   * Issue #4400: set on an AGGREGATE figure when at least one contributor is
   * `unknown`, so the amount is a **lower bound**. `costTooltip` already took
   * this as an option; the API now sends it on the figure itself
   * (`CostFigure.partial` in `src/budget/schemas.py`), so the caller no longer
   * has to know to pass it — which is how a partial total would get rendered as
   * an exact one.
   */
  partial?: boolean | null;
}

/**
 * Format a dollar amount with sub-cent precision where it matters.
 *
 * Four decimals below a cent, two at or above it: `$0.0012`, `$1.50`. A flat
 * `toFixed(2)` would render the majority of individual agent calls as `$0.00`,
 * which is the same lie by a different route.
 *
 * Exported for the `none_incurred`/`known` paths and for callers that have
 * already established the value is real. Prefer `formatCostFigure` when a status
 * is available — this function cannot express absence.
 */
export function formatAmount(amountUsd: number): string {
  if (Number.isNaN(amountUsd)) return NO_DATA_INDICATOR;
  // Exact zero is special-cased to `$0.00`, not `$0.0000`. A verified zero is a
  // round number and should read like one; four decimals of zeros imply a
  // measurement precise enough to have found something, which is the opposite of
  // what this value says. The issue names `$0.00` as the required rendering for
  // `none_incurred`, and the sub-cent branch below would otherwise catch 0 too.
  if (amountUsd === 0) return '$0.00';
  return amountUsd < 0.01 ? `$${amountUsd.toFixed(4)}` : `$${amountUsd.toFixed(2)}`;
}

/**
 * Format a possibly-absent cost. **This is the function to reach for.**
 *
 * Returns `NO_DATA_INDICATOR` for `null`/`undefined`/`NaN` rather than `$0.00`.
 * The distinction is the whole point: a missing figure and a real zero must not
 * render the same way.
 */
export function formatCost(amountUsd: number | null | undefined): string {
  if (amountUsd == null || Number.isNaN(amountUsd)) return NO_DATA_INDICATOR;
  return formatAmount(amountUsd);
}

/**
 * Format a three-valued cost figure from the API.
 *
 * `unknown` returns `NO_DATA_INDICATOR` even if an amount is somehow attached —
 * the status is authoritative over the number. A defensive choice, but this is
 * the exact seam where an `unknown` carrying `0` would become `$0.00` again.
 */
export function formatCostFigure(figure: CostFigure | null | undefined): string {
  if (!figure || figure.status === 'unknown') return NO_DATA_INDICATOR;
  const amount = figure.amount_usd == null ? NaN : Number(figure.amount_usd);
  if (Number.isNaN(amount)) return NO_DATA_INDICATOR;
  // `none_incurred` deliberately falls through to the formatter: a verified zero
  // SHOULD render as $0.00. That is a measurement, not a gap.
  return formatAmount(amount);
}

/**
 * Format a run's cost, distinguishing "still running" from "no data".
 *
 * A zero on an in-progress run means cost has not been backfilled yet, not that
 * the run was free — `ActivityCard` and `AgentActivity` both already made this
 * distinction locally, and it is preserved here rather than dropped in the
 * consolidation.
 */
export function formatRunCost(amountUsd: number | null | undefined, status?: string | null): string {
  if (amountUsd == null || Number.isNaN(amountUsd)) return NO_DATA_INDICATOR;
  if (amountUsd === 0 && status === 'in_progress') return 'pending';
  return formatAmount(amountUsd);
}

/**
 * Human-readable explanation for an `unknown` figure.
 *
 * A bare `'—'` with no explanation reads as a UI bug, so every `unknown` gets a
 * tooltip. Unmapped reasons degrade to a generic sentence rather than a blank:
 * the API and the SPA deploy independently, so an unfamiliar reason is normal.
 */
export function describeUnknownReason(reason: string | null | undefined): string {
  switch (reason) {
    case 'no_usage_rows':
      return 'No metered usage recorded for this work.';
    case 'not_started':
      return 'This work has not run yet, so nothing has been billed.';
    case 'not_costable':
      return 'This step makes no model calls, so there is nothing to bill.';
    case 'non_gateway_path':
      return 'This run called Bedrock directly, which records no usage row.';
    default:
      return 'Cost data unavailable for this item.';
  }
}

/**
 * Tooltip for a figure: the scope label, plus why it is unknown or partial.
 *
 * Returned as one string because every consumer renders it in a `title`
 * attribute. `partial` is called out explicitly — a partial total is a **lower
 * bound**, and presenting it as a total is how decisions get made on wrong
 * numbers.
 */
export function costTooltip(figure: CostFigure | null | undefined, options?: { partial?: boolean }): string {
  const parts: string[] = [];
  if (!figure || figure.status === 'unknown') {
    parts.push(describeUnknownReason(figure?.reason));
  }
  if (options?.partial) {
    parts.push('Partial total: some items have no cost data, so this is a lower bound.');
  }
  parts.push(figure?.scope ?? COST_SCOPE_LABEL);
  return parts.join(' ');
}
