/**
 * "Running now" tile for the Agent Run Dashboard.
 *
 * Issue #3633: Shows count of currently active runs.
 * Clicks through to /activity?status=in_progress.
 */

import { useNavigate } from 'react-router-dom';
import { StatCard } from '@/components/dashboard/StatCard';

interface ActiveRunsTileProps {
  count: number;
}

export function ActiveRunsTile({ count }: ActiveRunsTileProps) {
  const navigate = useNavigate();

  return (
    <div
      role="button"
      tabIndex={0}
      aria-label={`Running now: ${count}. Click to view active runs.`}
      className="cursor-pointer transition-shadow hover:shadow-md rounded-lg"
      onClick={() => navigate('/activity?status=in_progress')}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          navigate('/activity?status=in_progress');
        }
      }}
    >
      <StatCard
        title="Running now"
        value={count}
        icon="🔄"
        subtitle={count === 1 ? '1 active run' : `${count} active runs`}
      />
    </div>
  );
}
