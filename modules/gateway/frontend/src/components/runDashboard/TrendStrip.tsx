/**
 * 7-day trend strip for the Agent Run Dashboard.
 *
 * Issue #3771: Compact stacked-bar row showing daily completed/failed counts
 * from the `daily[]` array in RunStatsResponse. Height-capped at ~80px,
 * no external chart library — hand-rolled divs.
 *
 * Issue #3825: Added numerals above bars, legend below, active segment for
 * today's bucket, and sliced to exactly 7 days.
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

/**
 * Slice daily[] to the last 7 entries. Shared between TrendStrip and
 * WeekSummary so their totals can never diverge.
 */
export function sliceLast7Days(daily: TrendDay[]): TrendDay[] {
  if (daily.length <= 7) return daily;
  return daily.slice(daily.length - 7);
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

/**
 * Determine if a day is "today" (the last entry in the sliced array).
 * Active runs only make sense for today; past days should always show 0 active.
 */
function computeActive(day: TrendDay, isToday: boolean): number {
  if (!isToday) return 0;
  return Math.max(0, day.total - day.completed - day.failed);
}

export function TrendStrip({ daily }: TrendStripProps) {
  const navigate = useNavigate();

  const days = sliceLast7Days(daily);

  // Guard: hide when fewer than 2 days of data
  if (days.length < 2) {
    return null;
  }

  // Find the max total across all days for relative bar sizing
  const maxTotal = Math.max(...days.map((d) => d.total), 1);

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
        Past 7 days
      </p>
      <div className="flex items-end gap-1 h-20">
        {days.map((day, idx) => {
          const isToday = idx === days.length - 1;
          const active = computeActive(day, isToday);
          const completedPct = maxTotal > 0 ? (day.completed / maxTotal) * 100 : 0;
          const failedPct = maxTotal > 0 ? (day.failed / maxTotal) * 100 : 0;
          const activePct = maxTotal > 0 ? (active / maxTotal) * 100 : 0;
          const hasActivity = day.total > 0;

          const ariaParts = [
            `${formatDayLabel(day.date)}: ${day.completed} succeeded, ${day.failed} failed`,
          ];
          if (active > 0) {
            ariaParts[0] += `, ${active} running`;
          }

          return (
            <button
              key={day.date}
              type="button"
              onClick={() => handleDayClick(day.date)}
              className="flex-1 flex flex-col items-center gap-0.5 group cursor-pointer"
              title={ariaParts[0]}
              aria-label={ariaParts[0]}
            >
              {/* Total numeral above bar */}
              <span
                className="text-[10px] text-gray-400 dark:text-gray-500 leading-none"
                data-testid={`trend-bar-count-${day.date}`}
              >
                {day.total}
              </span>
              {/* Stacked bar */}
              <div className="w-full flex flex-col justify-end h-12">
                {hasActivity ? (
                  <div className="w-full flex flex-col justify-end h-full">
                    {active > 0 && (
                      <div
                        className="w-full bg-blue-300 dark:bg-blue-400 rounded-t-sm group-hover:bg-blue-400 dark:group-hover:bg-blue-300 transition-colors"
                        style={{ height: `${activePct}%`, minHeight: '2px' }}
                        data-testid={`trend-bar-active-${day.date}`}
                      />
                    )}
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
      {/* Legend */}
      <div
        className="flex items-center gap-3 mt-2 text-[10px] text-gray-400 dark:text-gray-500"
        data-testid="trend-strip-legend"
      >
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full bg-green-400 dark:bg-green-500" />
          Succeeded
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full bg-red-400 dark:bg-red-500" />
          Failed
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block w-2 h-2 rounded-full bg-blue-300 dark:bg-blue-400" />
          Running
        </span>
        <span className="text-gray-300 dark:text-gray-600">—</span>
        <span>runs per day</span>
      </div>
    </div>
  );
}
