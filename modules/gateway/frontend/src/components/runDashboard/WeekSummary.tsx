/**
 * Week summary context line for the Agent Run Dashboard.
 *
 * Issue #3771: Shows "This week: $N across M runs" using window spend
 * and sum of daily totals. Provides at-a-glance weekly context that the
 * today-only tiles miss.
 *
 * Displays "—" when spend is null (data unavailable).
 */

import { formatCurrency } from '@/utils/format';
import type { TrendDay } from './TrendStrip';
import type { RunStatsSpend } from '@/services/runStats';

interface WeekSummaryProps {
  daily: TrendDay[];
  spend: RunStatsSpend | null;
}

export function WeekSummary({ daily, spend }: WeekSummaryProps) {
  const totalRuns = daily.reduce((sum, d) => sum + d.total, 0);

  // Hide if there's nothing meaningful to show
  if (totalRuns === 0 && spend === null) {
    return null;
  }

  const spendDisplay = spend !== null ? formatCurrency(spend.total_cost_usd) : '—';

  return (
    <p
      className="text-sm text-gray-500 dark:text-gray-400"
      data-testid="week-summary"
    >
      This week:{' '}
      <span className="font-medium text-gray-700 dark:text-gray-300">
        {spendDisplay}
      </span>
      {' '}across{' '}
      <span className="font-medium text-gray-700 dark:text-gray-300">
        {totalRuns} {totalRuns === 1 ? 'run' : 'runs'}
      </span>
    </p>
  );
}
