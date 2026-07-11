/**
 * "Spend today" tile for the Agent Run Dashboard.
 *
 * Issue #3633: Shows today's spend. When spend is null (data unavailable),
 * displays "—" with a tooltip instead of misleading "$0.00" (REQ-A5).
 * Informational only — no click action.
 */

import { StatCard } from '@/components/dashboard/StatCard';
import { formatCurrency } from '@/utils/format';

interface SpendTodayTileProps {
  spend: number | null;
}

export function SpendTodayTile({ spend }: SpendTodayTileProps) {
  const displayValue = spend === null ? '—' : formatCurrency(spend);

  return (
    <div
      aria-label={
        spend === null
          ? 'Spend today: Cost data temporarily unavailable'
          : `Spend today: ${displayValue}`
      }
      title={spend === null ? 'Cost data temporarily unavailable' : undefined}
    >
      <StatCard
        title="Spend today"
        value={displayValue}
        icon="💰"
        subtitle={spend === null ? 'Cost data temporarily unavailable' : undefined}
      />
    </div>
  );
}
