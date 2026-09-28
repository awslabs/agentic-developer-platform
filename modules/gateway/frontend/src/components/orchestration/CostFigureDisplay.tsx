/**
 * A three-valued cost figure, with its scope label (issue #4212, AC-4/AC-22).
 *
 * Every rule here is already implemented in `utils/cost.ts` and this component
 * only composes it — a second formatter is how five of them happened before
 * #4207 consolidated the lot.
 *
 * The two invariants this guarantees at the render layer:
 *
 * 1. **`unknown` never renders `$0.00`.** `formatCostFigure` returns the
 *    `—` indicator for `unknown` even if an amount is somehow attached, and every
 *    `unknown` carries a `title` explaining *why* — a bare dash reads as a UI bug.
 * 2. **Every figure carries its scope.** These totals are agent-run Bedrock spend
 *    only; a figure that silently excludes build and infra cost reads as the total,
 *    and someone will make a budget decision on it.
 */

import { formatCostFigure, describeUnknownReason, COST_SCOPE_LABEL, type CostFigure } from '@/utils/cost';
import type { AggregateCostFigure } from '@/types/orchestration';

export interface CostFigureDisplayProps {
  figure: CostFigure | AggregateCostFigure | null | undefined;
  /** Prefix, e.g. "Total". Part of the accessible name, not decoration. */
  label?: string;
  /**
   * Render the scope label as visible text rather than only a tooltip. Used for
   * the flow rollup, where the headline number is the one most likely to be
   * screenshotted into a budget conversation without its caveat.
   */
  showScope?: boolean;
  className?: string;
}

export function CostFigureDisplay({ figure, label, showScope = false, className = '' }: CostFigureDisplayProps) {
  const rendered = formatCostFigure(figure);
  const isUnknown = !figure || figure.status === 'unknown';
  const scope = figure?.scope || COST_SCOPE_LABEL;

  // A partial aggregate is a **lower bound**, not a total (AC-21): some member
  // node's cost is unmeasured, so the sum omits a real contribution.
  const partial = Boolean(figure && 'partial' in figure && figure.partial) && !isUnknown;

  const title = isUnknown ? `${describeUnknownReason(figure?.reason)} (${scope})` : `${partial ? 'At least this much — some work has no recorded cost. ' : ''}${scope}`;

  return (
    <span className={`inline-flex items-baseline gap-1 ${className}`} data-testid="cost-figure" title={title}>
      {label && <span className="text-xs text-gray-500 dark:text-gray-400">{label}</span>}
      <span
        className="font-medium tabular-nums text-gray-900 dark:text-gray-100"
        data-cost-status={figure?.status ?? 'unknown'}
      >
        {partial ? '≥' : ''}
        {rendered}
      </span>
      {showScope && <span className="text-xs font-normal text-gray-500 dark:text-gray-400">({scope})</span>}
    </span>
  );
}

export default CostFigureDisplay;
