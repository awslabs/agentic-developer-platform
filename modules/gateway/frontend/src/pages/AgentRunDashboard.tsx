/**
 * Agent Run Dashboard page.
 *
 * Issue #3633: Landing page at /runs showing 4 stat tiles,
 * recent failures list, loading skeletons, and guided empty state.
 */

import { Link, useNavigate } from 'react-router-dom';
import { useRunStats } from '@/hooks/useRunStats';
import { Card } from '@/components/ui';
import {
  ActiveRunsTile,
  FailedTodayTile,
  SpendTodayTile,
  SucceededTodayTile,
  EmptyState,
  TrendStrip,
  WeekSummary,
} from '@/components/runDashboard';
import { formatRelativeTime } from '@/utils/format';

function LoadingSkeleton() {
  return (
    <div data-testid="loading-skeleton">
      {/* Tile grid skeleton */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4 mb-8">
        {[1, 2, 3, 4].map((i) => (
          <div
            key={i}
            className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 animate-pulse"
          >
            <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-24 mb-4" />
            <div className="h-8 bg-gray-200 dark:bg-gray-700 rounded w-16" />
          </div>
        ))}
      </div>
      {/* Recent failures skeleton */}
      <div className="bg-white dark:bg-gray-800 rounded-lg shadow p-6 animate-pulse">
        <div className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-32 mb-4" />
        <div className="space-y-3">
          {[1, 2, 3].map((i) => (
            <div key={i} className="h-4 bg-gray-200 dark:bg-gray-700 rounded w-full" />
          ))}
        </div>
      </div>
    </div>
  );
}

export default function AgentRunDashboard() {
  const navigate = useNavigate();
  const { data, isLoading, error, refetch } = useRunStats(7);
  // Separate 1-day window for the "Spend today" tile — the API's spend
  // aggregate covers the whole requested window, so today's spend needs
  // its own query (both responses are served from the backend's 60s cache).
  const { data: todayData } = useRunStats(1);

  // Determine if we should show empty state
  const isEmpty =
    data && data.today.total === 0 && data.active_runs.length === 0;

  return (
    <div className="space-y-6">
      {/* Page header */}
      <div>
        <h1 className="text-2xl font-bold text-gray-900 dark:text-white">
          Dashboard
        </h1>
        <p className="text-sm text-gray-500 dark:text-gray-400 mt-1">
          Your agent runs at a glance — see{' '}
          <Link
            to="/activity"
            className="text-primary-600 dark:text-primary-400 hover:underline"
          >
            Activity
          </Link>{' '}
          for the full history
        </p>
      </div>

      {/* Error state */}
      {error && (
        <div className="bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg p-4">
          <p className="text-red-700 dark:text-red-300">
            {error instanceof Error ? error.message : 'Failed to load run stats'}
          </p>
          <button
            onClick={() => refetch()}
            className="mt-2 text-sm text-red-600 dark:text-red-400 underline hover:no-underline"
          >
            Retry
          </button>
        </div>
      )}

      {/* Loading state */}
      {isLoading && <LoadingSkeleton />}

      {/* Empty state */}
      {!isLoading && !error && isEmpty && <EmptyState />}

      {/* Data state */}
      {!isLoading && !error && data && !isEmpty && (
        <>
          {/* Tile grid */}
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
            <ActiveRunsTile count={data.active_runs.length} />
            <FailedTodayTile count={data.today.failed} />
            <SpendTodayTile spend={todayData?.spend ? todayData.spend.total_cost_usd : null} />
            <SucceededTodayTile count={data.today.completed} />
          </div>

          {/* 7-day trend strip + week summary (Issue #3771) */}
          <TrendStrip daily={data.daily} />
          <WeekSummary daily={data.daily} spend={data.spend} />

          {/* Recent failures */}
          {data.recent_failures.length > 0 && (
            <Card>
              <h2 className="text-lg font-semibold text-gray-900 dark:text-white mb-4">
                Recent Failures
              </h2>
              <ul className="divide-y divide-gray-200 dark:divide-gray-700">
                {data.recent_failures.slice(0, 5).map((failure) => (
                  <li key={failure.invocation_id} className="py-3">
                    <button
                      onClick={() =>
                        navigate(`/activity?id=${failure.invocation_id}`)
                      }
                      className="w-full text-left hover:bg-gray-50 dark:hover:bg-gray-700/50 rounded px-2 py-1 -mx-2 transition-colors"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium text-gray-900 dark:text-white truncate">
                          {failure.topic || 'Untitled run'}
                        </span>
                        <span className="text-xs text-gray-500 dark:text-gray-400 ml-2 whitespace-nowrap">
                          {formatRelativeTime(failure.invoked_at)}
                        </span>
                      </div>
                      {failure.persona && (
                        <span className="text-xs text-gray-500 dark:text-gray-400">
                          {failure.persona}
                        </span>
                      )}
                    </button>
                  </li>
                ))}
              </ul>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
