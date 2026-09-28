/**
 * Shared cost badges (issue #4400).
 *
 * `utils/cost.ts` (#4207) unified how a cost is *formatted*. This unifies how it
 * is *styled*: `CostBadge` and `ChainCostBadge` were local to
 * `pages/AgentActivity.tsx`, and the budget drill-down (#4400) needs the same
 * rendering. A third copy is where the no-data policy would drift — and the
 * failure mode of drift here is specific and bad: a badge that renders an absent
 * cost as `$0.00` tells an operator the work was free.
 *
 * The division of labour is deliberate. These components decide **only** colour,
 * font and tooltip placement; every question about *what* to show — sub-cent
 * precision, the `'—'` convention, `pending`, the scope disclaimer — is answered
 * by `utils/cost.ts` and never re-decided here.
 */

import { formatCost, formatRunCost, formatCostFigure, costTooltip, NO_DATA_INDICATOR, COST_SCOPE_LABEL } from '@/utils/cost';
import type { CostFigure } from '@/utils/cost';
import type { InvocationItem } from '@/types/activity';

/** Shared styling, so the three badges cannot drift apart on font or colour. */
const MUTED_CLASS = 'text-gray-400 dark:text-gray-500 text-sm';
const AMOUNT_CLASS = 'text-sm text-gray-900 dark:text-white font-mono';

/**
 * Per-run cost from a legacy numeric field (`InvocationItem.total_cost_usd`).
 *
 * The `'—'` case is not "zero" — it is "not metered, or no usage row yet" — and
 * it is muted with the scope tooltip rather than rendered as a currency string.
 * `pending` is italic because it is a statement about time, not about money.
 *
 * `formatRunCost` owns the decision of which case applies; this only styles it.
 */
export function CostBadge({ item }: { item: InvocationItem }) {
  const formatted = formatRunCost(item.total_cost_usd, item.status);

  if (formatted === NO_DATA_INDICATOR) {
    return (
      <span className={MUTED_CLASS} title={COST_SCOPE_LABEL}>
        {formatted}
      </span>
    );
  }
  if (formatted === 'pending') {
    return <span className={`${MUTED_CLASS} italic`}>{formatted}</span>;
  }
  return (
    <span className={AMOUNT_CLASS} title={`${item.call_count ?? 0} calls, ${item.total_tokens ?? 0} tokens — ${COST_SCOPE_LABEL}`}>
      {formatted}
    </span>
  );
}

/** Aggregate cost for a chain, from a legacy numeric field. */
export function ChainCostBadge({ cost }: { cost: number | null }) {
  const formatted = formatCost(cost);
  const isMissing = formatted === NO_DATA_INDICATOR;
  return (
    <span className={isMissing ? MUTED_CLASS : AMOUNT_CLASS} title={COST_SCOPE_LABEL}>
      {formatted}
    </span>
  );
}

interface CostFigureBadgeProps {
  figure: CostFigure | null | undefined;
  /**
   * Set on an AGGREGATE figure whose `partial` flag is true, so the tooltip says
   * the number is a lower bound. Read from the figure itself when omitted.
   */
  partial?: boolean;
  className?: string;
}

/**
 * A three-valued `CostFigure` from the API (`{status, amount_usd, reason, partial}`).
 *
 * This is the badge the budget drill-down uses, and the one to prefer in new
 * code: the API sends a status precisely so the client never has to infer
 * "unknown" from a zero. `formatCostFigure` treats the **status as
 * authoritative**, so a malformed `{status: 'unknown', amount_usd: '0'}` still
 * renders `'—'` rather than `$0.00`.
 *
 * `unknown` and `partial` always carry a tooltip. A bare `'—'` with no
 * explanation reads as a UI bug, and an unexplained partial total reads as an
 * exact one — which is how a budget decision gets made on a lower bound.
 */
export function CostFigureBadge({ figure, partial, className }: CostFigureBadgeProps) {
  const formatted = formatCostFigure(figure);
  const isPartial = partial ?? figure?.partial ?? false;
  const isMissing = formatted === NO_DATA_INDICATOR;

  return (
    <span
      className={className ?? (isMissing ? MUTED_CLASS : AMOUNT_CLASS)}
      title={costTooltip(figure, { partial: isPartial })}
      data-cost-status={figure?.status ?? 'unknown'}
      data-cost-partial={isPartial ? 'true' : 'false'}
    >
      {formatted}
      {/* A partial total is a lower bound. The marker is visible, not tooltip-only:
          the tooltip explains it, but someone reading the number at a glance has
          to be able to see that it is incomplete. */}
      {isPartial && !isMissing && (
        <>
          <span aria-hidden="true">+</span>
          <span className="sr-only"> or more — partial total</span>
        </>
      )}
    </span>
  );
}
