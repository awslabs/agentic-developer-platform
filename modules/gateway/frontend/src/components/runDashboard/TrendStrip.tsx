/**
 * 7-day trend strip for the Agent Run Dashboard.
 *
 * Issue #3771: Compact stacked-bar row showing daily completed/failed counts
 * from the `daily[]` array in RunStatsResponse. Height-capped at ~80px,
 * no external chart library — hand-rolled divs.
 *
 * Behavior:
 * - Hidden when fewer than 2 days of data (guard for new deployments).
 * - Clicking a day navigates to /activity?view=runs&since=<date>&until=<date>.
 */

import { useNavigate } from 'react-router-dom';

export interface TrendDay {
  date: string;   // "YYYY-MM-DD"
  total: number;
  completed: number;
  failed: number;
}

interface TrendStripProps {
  daily: TrendDay[];
}

/**
 * Format a YYYY-MM-DD date string to a short day label (e.g., "Mon", "Tue").
 */
function formatDayLabel(dateStr: string): string {
  const d = new Date(dateStr + 'T00:00:00');
  return d.toLocaleDateString('en-US', { weekday: 'short' });
}

export function TrendStrip({ daily }: TrendStripProps) {
  const navigate = useNavigate();

  // Guard: hide when fewer than 2 days of data
  if (daily.length < 2) {
    return null;
  }

  // Find the max total across all days for relative bar sizing
  const maxTotal = Math.max(...daily.map((d) => d.total), 1);

  function handleDayClick(dateStr: string) {
    navigate(`/activity?view=runs&since=${dateStr}&until=${dateStr}`);
  }

  return (
    <div
      className="bg-white dark:bg-gray-800 rounded-lg shadow px-4 py-3"
      aria-label="7-day activity trend"
      data-testid="trend-strip"
    >
      <p className="text-xs font-medium text-gray-500 dark:text-gray-400 mb-2">
        Past {daily.length} days
      </p>
      <div className="flex items-end gap-1 h-16">
        {daily.map((day) => {
          const completedPct = maxTotal > 0 ? (day.completed / maxTotal) * 100 : 0;
          const failedPct = maxTotal > 0 ? (day.failed / maxTotal) * 100 : 0;
          const hasActivity = day.total > 0;

          return (
            <button
              key={day.date}
              type="button"
              onClick={() => handleDayClick(day.date)}
              className="flex-1 flex flex-col items-center gap-0.5 group cursor-pointer"
              title={`${formatDayLabel(day.date)}: ${day.completed} succeeded, ${day.failed} failed`}
              aria-label={`${formatDayLabel(day.date)}: ${day.completed} succeeded, ${day.failed} failed`}
            >
              {/* Stacked bar */}
              <div className="w-full flex flex-col justify-end h-12">
                {hasActivity ? (
                  <div className="w-full flex flex-col justify-end h-full">
                    {day.failed > 0 && (
                      <div
                        className="w-full bg-red-400 dark:bg-red-500 rounded-t-sm group-hover:bg-red-500 dark:group-hover:bg-red-400 transition-colors"
                        style={{ height: `${failedPct}%`, minHeight: day.failed > 0 ? '2px' : '0' }}
                      />
                    )}
                    {day.completed > 0 && (
                      <div
                        className="w-full bg-green-400 dark:bg-green-500 rounded-b-sm group-hover:bg-green-500 dark:group-hover:bg-green-400 transition-colors"
                        style={{ height: `${completedPct}%`, minHeight: day.completed > 0 ? '2px' : '0' }}
                      />
                    )}
                  </div>
                ) : (
                  <div className="w-full bg-gray-100 dark:bg-gray-700 rounded-sm h-1 mt-auto" />
                )}
              </div>
              {/* Day label */}
              <span className="text-[10px] text-gray-400 dark:text-gray-500 leading-none">
                {formatDayLabel(day.date)}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
