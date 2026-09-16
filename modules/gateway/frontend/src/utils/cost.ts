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
 * Format a money value that arrived as a **wire string** (issue #4685).
 *
 * Distinct from `formatAmount`/`formatCost` above, which take a `number`: money on
 * the budget wire is a string at the column's own precision (caps `NUMERIC(10,2)`,
 * spend `NUMERIC(14,6)`) precisely so sub-cent digits survive JSON, and a
 * number-typed entry point is where that precision quietly dies. Parsing happens
 * here, for display only — nothing downstream compares a rounded figure to a cap.
 *
 * This is the single copy. There were four byte-divergent private ones
 * (`BudgetLines`, `PerOrgSpend`, `PersonSpendingLimit`, `pages/BudgetSpend`) and
 * they disagreed on the cases that matter: two rendered `''` as `$0.00` (because
 * `Number('')` is `0`, not `NaN`), two rendered `'-0.004'` as `-$0.00`, and two
 * flattened real sub-cent spend to `$0.00` on the very rows whose purpose is
 * disproving a `$0` reading.
 *
 * The contract, in the order the branches run:
 *
 * 1. `null`/`undefined`/blank → `NO_DATA_INDICATOR`. **Never `$0.00`** — this is
 *    called on `cap_usd`/`remaining_usd`, legitimately `null` on an uncapped line,
 *    and "no cap configured" must not read as "no money left".
 * 2. non-finite (`NaN`, `'Infinity'`, `'1e999'`) → `NO_DATA_INDICATOR`. An
 *    `isNaN` check alone passes `Infinity` and renders `$Infinity`.
 * 3. non-zero below a cent → **4dp**, so `'0.000412'` is not reported as `$0.00`.
 *    Real per-request costs are genuinely sub-cent, and flattening them to `$0.00`
 *    on the very rows whose purpose is disproving a `$0` reading is self-defeating.
 * 4. otherwise 2dp.
 *
 * In both money branches the sign is taken from the value **as rounded for display**,
 * never from the input. Otherwise a magnitude too small to survive its own precision
 * renders as a signed zero — `'-0.00000001'` as `-$0.0000` — which reads as a debt of
 * nothing and is the only way this function can print a minus sign it cannot justify.
 */
/**
 * Parse a wire money string to a number, or `null` when it is not a measurement.
 *
 * The single definition of "readable" for wire money (review fix on #4686):
 * consumers that need the numeric value — a progress-bar position, an
 * unreadable-figure caveat — must gate on THIS, not on `== null`, or an empty
 * string (`Number('') === 0`) renders "we could not read your spend" and "0% of
 * your limit used" on the same card.
 */
export function parseWireMoney(value: string | null | undefined): number | null {
  if (value == null || value.trim() === '') return null;
  const amount = Number(value);
  if (!Number.isFinite(amount)) return null;
  // `toFixed` switches to exponential notation at 1e21 and float precision is
  // garbage long before that; no real money reaches 1e15. Beyond it the value is
  // corruption, not currency — unreadable, never `'$1e+21'`.
  if (Math.abs(amount) >= 1e15) return null;
  return amount;
}

export function formatWireMoney(value: string | null | undefined): string {
  const amount = parseWireMoney(value);
  if (amount == null) return NO_DATA_INDICATOR;
  // Sub-cent is decided on the ROUNDED value (review fix on #4686): '0.00999' is
  // one cent after rounding and must render '$0.01' — deciding on the raw value
  // produced two spellings of the same cent ('$0.0100' vs '$0.01'). NOTE: this
  // deliberately differs from `formatAmount` above, which takes an already-numeric
  // per-token cost and keeps 4dp precision for values this function would call
  // zero; the two serve different columns and must not be merged blindly.
  const rounded2 = Number(amount.toFixed(2));
  if (rounded2 !== 0 || amount === 0) {
    const sign = rounded2 < 0 ? '-' : '';
    return `${sign}$${Math.abs(rounded2).toFixed(2)}`;
  }
  // Rounds to zero at 2dp but is not zero: show 4dp so real sub-cent spend never
  // reads as `$0.00` — unless even 4dp carries no figure.
  const rounded4 = Number(amount.toFixed(4));
  if (rounded4 === 0) return '$0.00';
  const sign = rounded4 < 0 ? '-' : '';
  return `${sign}$${Math.abs(rounded4).toFixed(4)}`;
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
