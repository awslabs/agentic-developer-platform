/**
 * "Spend today" tile for the Agent Run Dashboard.
 *
 * Issue #3633: Shows today's spend. When spend is null (data unavailable),
 * displays "—" with a tooltip instead of misleading "$0.00" (REQ-A5).
 * Issue #3766: Clickable — navigates to /activity?view=runs.
 */

import { useNavigate } from 'react-router-dom';
import { StatCard } from '@/components/dashboard/StatCard';
import { formatCurrency } from '@/utils/format';

interface SpendTodayTileProps {
  spend: number | null;
}

export function SpendTodayTile({ spend }: SpendTodayTileProps) {
  const navigate = useNavigate();
  const displayValue = spend === null ? '—' : formatCurrency(spend);

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={
        spend === null
          ? 'Spend today: Cost data temporarily unavailable. Click to view run costs.'
          : `Spend today: ${displayValue}. Click to view run costs.`
      }
      title={spend === null ? 'Cost data temporarily unavailable' : undefined}
      className="cursor-pointer transition-shadow hover:shadow-md rounded-lg"
      onClick={() => navigate('/activity?view=runs')}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate('/activity?view=runs');
        }
      }}
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
