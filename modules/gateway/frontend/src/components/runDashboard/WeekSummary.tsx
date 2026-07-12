/**
 * Week summary context line for the Agent Run Dashboard.
 *
 * Issue #3771: Shows spend and run count for the trend window.
 * Issue #3825: Uses the same sliceLast7Days helper as TrendStrip so
 * the run count always matches the visible bars.
 *
 * Displays "—" when spend is null (data unavailable).
 */

import { formatCurrency } from '@/utils/format';
import { sliceLast7Days } from './TrendStrip';
import type { TrendDay } from './TrendStrip';
import type { RunStatsSpend } from '@/services/runStats';

interface WeekSummaryProps {
  daily: TrendDay[];
  spend: RunStatsSpend | null;
}

export function WeekSummary({ daily, spend }: WeekSummaryProps) {
  const days = sliceLast7Days(daily);
  const totalRuns = days.reduce((sum, d) => sum + d.total, 0);

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
      Past 7 days:{' '}
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
