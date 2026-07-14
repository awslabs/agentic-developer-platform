/**
 * "Failed today" tile for the Agent Run Dashboard.
 *
 * Issue #3633: Shows count of today's failed runs.
 * Clicks through to /activity?status=failed&since=today.
 */

import { useNavigate } from 'react-router-dom';
import { StatCard } from '@/components/dashboard/StatCard';

interface FailedTodayTileProps {
  count: number;
}

export function FailedTodayTile({ count }: FailedTodayTileProps) {
  const navigate = useNavigate();

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Failed today: ${count}. Click to view failed runs.`}
      className="cursor-pointer transition-shadow hover:shadow-md rounded-lg"
      onClick={() => navigate('/activity?status=failed&since=today')}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate('/activity?status=failed&since=today');
        }
      }}
    >
      <StatCard
        title="Failed today"
        value={count}
        icon="❌"
        className={count > 0 ? 'border-l-4 border-red-500' : ''}
      />
    </div>
  );
}
