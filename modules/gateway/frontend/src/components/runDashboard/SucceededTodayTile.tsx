/**
 * "Succeeded today" tile for the Agent Run Dashboard.
 *
 * Issue #3633: Shows count of today's succeeded runs.
 * Clicks through to /activity?status=complete&since=today.
 */

import { useNavigate } from 'react-router-dom';
import { StatCard } from '@/components/dashboard/StatCard';

interface SucceededTodayTileProps {
  count: number;
}

export function SucceededTodayTile({ count }: SucceededTodayTileProps) {
  const navigate = useNavigate();

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Succeeded today: ${count}. Click to view completed runs.`}
      className="cursor-pointer transition-shadow hover:shadow-md rounded-lg"
      onClick={() => navigate('/activity?status=complete&since=today')}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate('/activity?status=complete&since=today');
        }
      }}
    >
      <StatCard
        title="Succeeded today"
        value={count}
        icon="✅"
      />
    </div>
  );
}
